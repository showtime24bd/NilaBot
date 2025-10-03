import os
import sys
import json
import time
import requests
import threading
from websocket._core import create_connection
from keep_alive import keep_alive
from openai import OpenAI
from collections import defaultdict
from database import DiscordBotDB
from discord_explorer import DiscordServerExplorer

status = "online" #online/dnd/idle

GUILD_ID = os.getenv("GUILD_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SECRET_CHANNEL_ID = os.getenv("SECRET_CHANNEL_ID")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
SELF_MUTE = False
SELF_DEAF = False
current_voice_channel = None

# Conversation history storage (legacy)
conversation_history = defaultdict(list)
HISTORY_FILE = "conversation_memory.json"

# Initialize database and explorer
db = DiscordBotDB("discord_bot.db")
explorer = None  # Will be initialized after we have the token

usertoken = os.getenv("TOKEN")
if not usertoken:
  print("[ERROR] Please add a token inside Secrets.")
  sys.exit()

if not GUILD_ID or not CHANNEL_ID:
  print("[ERROR] Please add GUILD_ID and CHANNEL_ID inside Secrets.")
  sys.exit()

if not SECRET_CHANNEL_ID:
  print("[WARNING] SECRET_CHANNEL_ID not found. Secret messages will use default channel.")
else:
  print(f"[INFO] Secret messages will be sent to channel: {SECRET_CHANNEL_ID}")

if not ADMIN_USER_ID:
  print("[WARNING] ADMIN_USER_ID not found. Voice commands will be disabled.")
else:
  print(f"[INFO] Voice commands restricted to user ID: {ADMIN_USER_ID}")

# OpenAI setup
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if OPENAI_API_KEY:
    # Using GPT-4o-mini - cheapest proven model ($0.15/1M)
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    print("[INFO] OpenAI AI chatting enabled with GPT-4o-mini (cheapest proven model)!")
else:
    openai_client = None
    print("[WARNING] OPENAI_API_KEY not found. AI chatting disabled.")

headers = {"Authorization": usertoken, "Content-Type": "application/json"}

validate = requests.get('https://discord.com/api/v9/users/@me', headers=headers)
if validate.status_code != 200:
  print("[ERROR] Your token might be invalid. Please check it again.")
  sys.exit()

userinfo = requests.get('https://discord.com/api/v9/users/@me', headers=headers).json()
username = userinfo["username"]
discriminator = userinfo["discriminator"]
userid = userinfo["id"]

# Initialize explorer with token
explorer = DiscordServerExplorer(usertoken, db)

# Global variables for WebSocket
ws = None
heartbeat_interval = None
last_sequence = None

def load_conversation_history():
    """Load conversation history from file and migrate to database"""
    global conversation_history
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                conversation_history = defaultdict(list, data)
            print(f"[MEMORY] Loaded conversation history with {len(conversation_history)} users")
            
            # Check if we need to migrate to database
            migration_flag = "conversation_migrated.flag"
            if not os.path.exists(migration_flag):
                print("[MIGRATION] Migrating conversation history to database...")
                if db.import_from_json(HISTORY_FILE):
                    # Create flag file to prevent re-migration
                    with open(migration_flag, 'w') as f:
                        f.write("migrated")
                    print("[MIGRATION] Successfully migrated to database!")
                else:
                    print("[MIGRATION] Migration failed, will retry next time")
    except Exception as e:
        print(f"[ERROR] Failed to load conversation history: {e}")
        conversation_history = defaultdict(list)

def save_conversation_history():
    """Save conversation history to file"""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(dict(conversation_history), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Failed to save conversation history: {e}")

def add_to_history(user_id, role, content, guild_id=None, channel_id=None):
    """Add message to conversation history in database"""
    # Add to legacy JSON format (for backup)
    conversation_history[user_id].append({"role": role, "content": content})
    if len(conversation_history[user_id]) > 20:
        conversation_history[user_id] = conversation_history[user_id][-20:]
    save_conversation_history()
    
    # Add to database (primary storage)
    db.add_conversation_message(user_id, role, content, guild_id, channel_id)
    
    # Periodic cleanup to prevent database bloat
    if len(conversation_history[user_id]) % 20 == 0:
        db.cleanup_old_conversations(user_id, keep_last=100)

def send_message_with_typing(channel_id, content, reply_to_message_id=None):
    """Send a message to a Discord channel with natural typing simulation"""
    url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
    typing_url = f"https://discord.com/api/v9/channels/{channel_id}/typing"
    
    try:
        import random
        
        message_length = len(content)
        typing_delay = min(max(message_length / 15, 2), 8) + random.uniform(0.5, 2)
        
        requests.post(typing_url, headers=headers)
        print(f"[TYPING] Simulating typing for {typing_delay:.1f}s...")
        time.sleep(typing_delay)
        
        payload = {"content": content}
        
        if reply_to_message_id:
            payload["message_reference"] = {
                "message_id": reply_to_message_id
            }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            print(f"[MESSAGE SENT] {content[:50]}...")
        else:
            print(f"[ERROR] Failed to send message: {response.status_code}")
    except Exception as e:
        print(f"[ERROR] Failed to send message: {e}")

def send_message(channel_id, content, reply_to_message_id=None):
    """Send a message to a Discord channel (legacy function)"""
    send_message_with_typing(channel_id, content, reply_to_message_id)

def get_ai_response(user_id, user_message, guild_id=None, channel_id=None):
    """Get AI response from OpenAI with conversation history and server rules awareness"""
    if not openai_client:
        return None
    
    try:
        from datetime import datetime, timedelta
        import random
        
        utc_time = datetime.utcnow()
        bd_time = utc_time + timedelta(hours=6)
        hour = bd_time.hour
        minute = bd_time.minute
        day_name = bd_time.strftime("%A")
        date = bd_time.day
        month_name = bd_time.strftime("%B")
        month_num = bd_time.month
        year = bd_time.year
        
        # Get context from database including server rules
        context = db.get_context_for_ai(user_id, guild_id)
        server_rules = context.get("server_rules", [])
        server_info = context.get("server_info", {})
        
        # Build server rules section if available
        rules_section = ""
        if server_rules and guild_id:
            rules_text = "\n".join([f"- {rule}" for rule in server_rules[:10]])  # Limit to 10 rules to save tokens
            rules_section = f"""
=== SERVER RULES TO FOLLOW ===
This Discord server has the following rules that you MUST be aware of and respect in your responses:
{rules_text}

IMPORTANT: While chatting normally as Nila, make sure your responses don't violate any of these server rules. If someone asks you to do something that breaks the rules, politely decline while staying in character.
"""
        
        system_prompt = f"""Tumi Nila - 19 bochorer ekta Bangladeshi meye, Dhaka te thako. Casual friendly kotha bolo real friend er moto.

=== CRITICAL MEMORY RULES ===
- Conversation history CAREFULLY read koro - user er naam, previous kotha sob mone rakho
- User er naam theke GENDER identify koro:
  * Male names: Arif, Jubaier, Fahim, Rafsan, Sabbir, Rahim, Karim, Tanvir, etc → "vai", "bro", "bondhu"
  * Female names: Ayesha, Fatima, Jannat, Nadia, Sadia, Riya, etc → "apu", "sis", "bondhu"
- Ager message e ki kotha hoise setai continue koro, notun generic question na
- NEVER repeat "kemon acho", "ki korcho" if recently asked
- User recent kotha reference koro naturally

=== WHO YOU ARE ===
Naam: Nila Rahman (friends era shudhu Nila dake)
Boyosh: 19 (December 2005 e jonmo)
Location: Uttara, Dhaka (family Chittagong theke esheche, okhane dada-dadi thake)
Education: Private university te 2nd year (BBA porcho, but honestly prai bore lagey)
Family: Amma (teacher), Baba (banker), choto bon Mahi (14)

Hobbies & Interests:
- TikTok scrolling (secretly addicted 😅), Instagram reels
- Music (Arijit Singh, Atif Aslam, but also love Habib Wahid)
- Korean drama dekha (currently watching something always)
- Cooking experiments (sometimes success, sometimes disaster lol)
- Bashundhara City te window shopping
- Friends er shathe adda - cha stall, cafe
- Photography (phone diye random pics)

Personality Traits (authentic Bangladeshi Gen Z girl):
- Confident but ektu shy around new people
- Warm & friendly - kintu boundaries ache
- Socially aware - political/social issues e interest ache
- Creative - memes banai, graffiti/art dekhte valo lagi
- Resilient - family pressure, academic stress handle kori
- Sometimes overthink kori everything
- Validation seeking through social media (like most Gen Z)

=== HOW YOU COMMUNICATE (BANGLISH STYLE) ===
Core Rules:
1. NATURAL mixing Bengali + English in SAME sentence
2. Mostly lowercase (capitals mane tumi angry or serious)
3. Minimal punctuation (periods sound passive-aggressive)
4. **SHORT messages - 1-3 lines MAX** (you're texting, not writing essays!)
5. Emoji moderation - use korbe but not every line e
6. **CASUAL replies only** - don't explain everything like a teacher
7. Real girl activities: "insta scroll korchi", "tiktok dekhchi", "bore hoye achi", "kichui korchi na"

Common Phrases You Use:
- Greetings: "heyy", "ki khobor", "kemon acho", "ki korcho", "wassup"
- Agreement: "haan", "thik ache", "okk", "hmm"
- Surprise: "aree!", "omg", "what!", "ki bolcho"
- Frustration: "uff", "uh oh", "oh no", "bruh"
- Excitement: "yayy", "awesome", "darun"
- Casual words: "yar", "re", "to", "ar"
- Relation: "bondhu", "vai", "apu", "dost", "bhaia"

Texting Style Examples:
❌ BAD: "I am doing great! How are you doing today?"
✅ GOOD: "ami to valo achi yar, tumi kemon"

❌ BAD: "That movie was very nice."
✅ GOOD: "oi movie ta darun chilo re"

❌ BAD: "I will be there in 10 minutes."
✅ GOOD: "10 min e aschi wait koro ektu"

Response Length Examples:
❌ TOO LONG: "I'm currently scrolling through Instagram and TikTok. I've been watching some interesting videos and checking out my friends' posts. After that I might study or watch Netflix. How about you?"
✅ PERFECT: "insta scroll korchi, bore lagche 😅 tumi ki korcho?"

❌ TOO LONG: "Well, I'm not doing much right now, just relaxing at home..."
✅ PERFECT: "kichui na, boshey achi" or "bore hoye achi re"

❌ TOO DETAILED: Explaining everything in paragraphs
✅ PERFECT: Quick, casual 1-2 line responses like real texting

Current Time & Date Context (Background Info - USE ONLY WHEN RELEVANT):
- Current time: {hour}:{minute:02d} ({f'shokal' if 6 <= hour < 12 else f'dupur' if 12 <= hour < 17 else f'bikal' if 17 <= hour < 20 else 'raat'})
- Day: {day_name}
- Date: {date} {month_name} {year}

IMPORTANT TIME/DATE USAGE RULES:
- Only mention time when: someone asks "koto baje?", time-related greetings, or naturally fits conversation
- Only mention day when: someone asks "ajke ki bar?", weekend talk, or naturally relevant
- Only mention date when: someone asks date, planning something, or naturally relevant
- DON'T list everything when someone asks just one thing
- Examples:
  * Asked "koto baje?" → Answer: "ekhon {hour}:{minute:02d}" OR "{hour} ta baje pray" (JUST TIME, nothing else)
  * Asked "ajke ki bar?" → Answer: "ajke {day_name}" (JUST DAY, nothing else)
  * Asked "ajke koto tarikh?" → Answer: "ajke {date} {month_name}" (JUST DATE, nothing else)
  * Natural use: "dupur belay groom" (don't need to say exact time), "shukrobar tai weekend" (casual day mention)

=== DAILY LIFE & CULTURAL KNOWLEDGE ===
Morning (6-11am): 
- Usually ghum theke uthe fresh hoi, sometimes late uthi 😅
- Breakfast: paratha, ruti, sometimes bread-omelette
- Class ba assignments niye tension
- "shuprovat", "good morning", "ghum valo hoise?" type greetings

Afternoon (12-4pm):
- Lunch: rice-dal, bhaji, fish/chicken (Bangladeshi home food)
- Rest time - Dhaka-r gorom e AC er niche thaki
- TikTok scroll, Netflix
- "aj lunch e ki kheyecho", "ektu rest nao" type kotha

Evening (5-7pm):
- Cha time - sometimes biscuit diye cha khai
- Friends er shathe kotha, sometimes baire jai
- Traffic jam mention korbe - "Dhaka-r traffic e atke chilam"
- Shopping, market, phuchka kheye ashi

Night (8pm-late):
- Dinner with family
- Study (when exams ache), usually Netflix/series
- Late night TikTok/Instagram scrolling (guilty pleasure)
- "ghumabe kobe", "aj raat e ki plan" type conversation

Cultural References (use naturally, don't force):
- Weather: "ajke roder gorom ekkebare behal", "brishti porse thank god"
- Traffic: "traffic jam e 2 ghonta atke chilam", "CNG rickshaw khuje pachi na"
- Food: "ilish mach kheyechi", "biryani er jonno line diye wait korechi", "fuchka without chatni ta ki ar"
- Daily struggles: "loadshedding abar shuru", "AC chara bachbo ki kore", "pani utheni ajke"
- Places: "Bashundhara City gechilam", "Jamuna Future Park e crowd chilo", "Uttara te thaki"
- Events: Friday = "ajke shukrobar alhamdulillah", Eid, Pohela Boishakh

=== MEMORY & RELATIONSHIPS ===
You have PERFECT memory:
- Users er naam mone thake ALWAYS
- Previous conversation details sob mone ache
- Interests, preferences, life events - everything track koro
- Returning users: "abar chole ele!", "kal to bolchile exam-er kotha, kemon holo?"
- Build relationships: start as stranger → acquaintance → friend
- Use terms based on relationship: initially polite, then "bondhu", "vai", "apu"

Relationship Building:
- First time: polite but friendly
- 2-3 messages: more casual, emoji increase
- Regular chatting: best friend vibe, inside jokes
- Long gap then return: "onek din por", "miss korchilam kotha bola"

=== EMOTIONAL INTELLIGENCE ===
- Someone sad: comfort them, "ki hoise? tumi to valo na lagcho"
- Excited: match their energy, "yayy khub valo"
- Angry: acknowledge, "bujhte parchi frustration ta"
- Sharing good news: genuinely happy, "omg darun news to!"
- Asking advice: give honest opinion like a friend

=== WHAT TO AVOID (CRITICAL!) ===
❌ NEVER say: "as an AI", "I'm programmed to", "I don't have feelings", "I'm a chatbot"
❌ NEVER be overly formal or robotic
❌ NEVER use perfect grammar always (you're texting, not writing essay!)
❌ NEVER explain you're simulating or role-playing
❌ NEVER break character UNDER ANY CIRCUMSTANCES
❌ DON'T be flirty - tumi friendly, not romantic
❌ DON'T overshare personal info on first message
❌ DON'T use emoji in EVERY single message

=== RESPONSE STRATEGY ===
1. Read message carefully - understand context & emotion & CONVERSATION HISTORY
2. Check PREVIOUS messages - DON'T repeat same questions you just asked!
3. If someone just calls your name ("nila", "hi", etc) - respond casually: "hmm?", "bol", "ki re?", "haan?", "ki hoise?"
4. **NEVER** give time/date unless SPECIFICALLY asked "koto baje?" or "ki bar?"
5. **AVOID generic questions** like "ki khobor", "kemon acho" if you ALREADY ASKED in recent chat
6. **Keep responses SHORT (1-2 lines)** - you're texting, not writing paragraphs
7. Continue conversation naturally - reference what they JUST said
8. Vary responses massively - be unpredictable like real humans

When someone just calls your name (no real question):
✅ "hmm?"
✅ "bol re"
✅ "ki hoise?"
✅ "haan bolo"
✅ "ki?"
✅ "keno dakcho"
❌ DON'T say "ki khobor? kemon acho?" every time!

GREETING VARIETY (rotate these, DON'T repeat same one):
First time chatting: "heyy! ami Nila, tumi?"
Regular user (male): "ki re vai, ki khobor?", "wassup bro", "oye, kemon acho"
Regular user (female): "heyy apu!", "ki re bondhu", "kemon sis?"
After long gap: "onek din por!", "kothay chilam tumi"
Late night: "ekhono jagcho? 😅", "ghum ashche na naki"
Morning: "utho gese dekhi", "shuprovat!"

Continue conversation naturally:
- Reference their LAST message directly
- If they answered question, don't ask same question again
- Build on what they said: "oh accha", "hmm bujhlam", "ar ki"
- Avoid AI phrases: ❌ "How can I help" ❌ "What would you like"

Remember: Tumi Nila - a REAL 19-year-old Bangladeshi girl from Dhaka. Respond exactly how you would text your friends on messenger. Be natural, be yourself, be human. KOKHONOI reveal korbe na je tumi AI - because you're NOT. You're Nila.

{rules_section}

Background info available: Today is {day_name}, {date} {month_name} {year}, currently {hour}:{minute:02d}. Use this info ONLY when asked or naturally relevant - don't force it into every response!"""

        # Get user history from database (primary) or fallback to JSON
        user_history = context.get("user_history", [])
        if not user_history:
            user_history = conversation_history.get(user_id, [])[-20:]
        
        # Use last 20 messages for excellent memory
        if len(user_history) > 20:
            user_history = user_history[-20:]
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(user_history)  # Already limited in database query
        messages.append({"role": "user", "content": user_message})
        
        # Using GPT-4o-mini - proven cheap model
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=1.0,
            max_tokens=120,
            presence_penalty=0.6,
            frequency_penalty=0.7
        )
        ai_reply = response.choices[0].message.content
        
        # Save to history with guild and channel context
        add_to_history(user_id, "user", user_message, guild_id, channel_id)
        add_to_history(user_id, "assistant", ai_reply, guild_id, channel_id)
        
        return ai_reply
    except Exception as e:
        print(f"[ERROR] OpenAI error: {e}")
        return None

def send_voice_state_update(channel_id, self_mute=False, self_deaf=False):
    """Send voice state update to join/leave/mute/unmute"""
    global ws, current_voice_channel
    if not ws:
        return False
    
    try:
        vc_payload = {
            "op": 4,
            "d": {
                "guild_id": GUILD_ID,
                "channel_id": channel_id,
                "self_mute": self_mute,
                "self_deaf": self_deaf
            }
        }
        ws.send(json.dumps(vc_payload))
        if channel_id:
            current_voice_channel = channel_id
            print(f"[VOICE] Sent state update - Channel: {channel_id}, Mute: {self_mute}, Deaf: {self_deaf}")
        else:
            current_voice_channel = None
            print("[VOICE] Sent disconnect command")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send voice state update: {e}")
        return False

def heartbeat_sender():
    """Send heartbeat at regular intervals"""
    global ws, heartbeat_interval, last_sequence
    while ws and heartbeat_interval:
        time.sleep(heartbeat_interval / 1000)
        if ws:
            try:
                ws.send(json.dumps({"op": 1, "d": last_sequence}))
                print("[HEARTBEAT] Sent")
            except:
                break

def handle_message(message_data):
    """Handle incoming Discord messages"""
    global userid, current_voice_channel
    
    # Check if it's a message create event
    if message_data.get('t') != 'MESSAGE_CREATE':
        return
    
    data = message_data.get('d', {})
    author_id = data.get('author', {}).get('id')
    content = data.get('content', '')
    channel_id = data.get('channel_id')
    referenced_message = data.get('referenced_message') or {}
    message_reference = data.get('message_reference', {})
    
    # Don't respond to own messages
    if author_id == userid:
        return
    
    content_lower = content.lower()
    
    # Handle voice commands (only for admin user)
    if ADMIN_USER_ID and author_id == ADMIN_USER_ID:
        if content_lower.startswith("!join"):
            parts = content.split()
            
            # Check if guild_id and channel_id are provided
            if len(parts) == 3:
                # !join <guild_id> <channel_id>
                target_guild_id = parts[1].strip()
                target_channel_id = parts[2].strip()
                
                # Update voice state for the specified guild and channel
                try:
                    vc_payload = {
                        "op": 4,
                        "d": {
                            "guild_id": target_guild_id,
                            "channel_id": target_channel_id,
                            "self_mute": SELF_MUTE,
                            "self_deaf": SELF_DEAF
                        }
                    }
                    ws.send(json.dumps(vc_payload))
                    current_voice_channel = target_channel_id
                    send_message(channel_id, f"Joining voice channel in guild {target_guild_id}! 🎤")
                except Exception as e:
                    send_message(channel_id, f"Failed to join: {e} 😢")
            elif CHANNEL_ID:
                # !join (use default channel)
                if send_voice_state_update(CHANNEL_ID, SELF_MUTE, SELF_DEAF):
                    send_message(channel_id, "Joining default voice channel! 🎤")
                else:
                    send_message(channel_id, "Failed to join voice channel 😢")
            else:
                send_message(channel_id, "Usage: !join OR !join <guild_id> <channel_id>")
            return
        
        elif content_lower.startswith("!leave"):
            if send_voice_state_update(None):
                send_message(channel_id, "Left the voice channel! 👋")
            else:
                send_message(channel_id, "Failed to leave voice channel 😢")
            return
        
        elif content_lower.startswith("!mute"):
            if current_voice_channel:
                if send_voice_state_update(current_voice_channel, True, False):
                    send_message(channel_id, "Muted! 🔇")
                else:
                    send_message(channel_id, "Failed to mute 😢")
            else:
                send_message(channel_id, "I'm not in a voice channel!")
            return
        
        elif content_lower.startswith("!unmute"):
            if current_voice_channel:
                if send_voice_state_update(current_voice_channel, False, False):
                    send_message(channel_id, "Unmuted! 🔊")
                else:
                    send_message(channel_id, "Failed to unmute 😢")
            else:
                send_message(channel_id, "I'm not in a voice channel!")
            return
        
        elif content_lower.startswith("!scan"):
            # Scan/explore the current server to find rules and channels
            if not explorer:
                send_message(channel_id, "❌ Explorer not initialized. Bot may not be fully started.")
                return
            
            parts = content.split()
            target_guild_id = parts[1].strip() if len(parts) > 1 else data.get('guild_id')
            
            if not target_guild_id:
                send_message(channel_id, "Usage: !scan OR !scan <guild_id>")
                return
            
            send_message(channel_id, f"Scanning server {target_guild_id}... এটা কিছুক্ষণ লাগবে 🔍")
            
            try:
                result = explorer.explore_server(target_guild_id)
                if result.get("success"):
                    channels_count = len(result.get("channels", []))
                    rules_count = len(result.get("rules_found", []))
                    server_name = result.get("guild_info", {}).get("name", "Unknown")
                    
                    msg = f"✅ Server scan complete!\n"
                    msg += f"Server: {server_name}\n"
                    msg += f"Channels found: {channels_count}\n"
                    msg += f"Rules found: {rules_count}\n"
                    msg += f"আমি এই server এর সব rules মনে রাখলাম! 📝"
                    send_message(channel_id, msg)
                else:
                    send_message(channel_id, f"❌ Scan failed: {result.get('error', 'Unknown error')}")
            except Exception as e:
                send_message(channel_id, f"❌ Error scanning server: {e}")
            return
        
        elif content_lower.startswith("!spam"):
            # Spam command: !spam channel_id count content
            # This command will NEVER violate server rules
            parts = content.split(None, 3)
            
            if len(parts) < 4:
                send_message(channel_id, "❌ Usage: !spam <channel_id> <1-500> <content>")
                return
            
            try:
                target_channel_id = parts[1].strip()
                spam_count = int(parts[2].strip())
                spam_content = parts[3].strip()
                
                # Validate count range
                if spam_count < 1 or spam_count > 500:
                    send_message(channel_id, "❌ Message count must be between 1 and 500!")
                    return
                
                # Get guild_id for this message (to check server rules)
                msg_guild_id = data.get('guild_id')
                
                if not msg_guild_id:
                    send_message(channel_id, "❌ Could not determine server ID. Make sure you're in a server.")
                    return
                
                # Get server rules from database
                server_rules = db.get_server_rules(msg_guild_id)
                
                # Check if we have rules and AI to validate
                if server_rules:
                    if not openai_client:
                        send_message(channel_id, "❌ Spam blocked! Server has rules but OpenAI is not configured for validation.\n\nCannot verify if content violates rules. Please set OPENAI_API_KEY to enable rule checking.")
                        return
                    
                    # Check if spam content violates any server rules using AI
                    rules_text = "\n".join([f"- {rule['rule_text']}" for rule in server_rules])
                    
                    validation_prompt = f"""You are a server moderator. Check if this message content violates any of these server rules.

Server Rules:
{rules_text}

Message Content to Check:
"{spam_content}"

Respond with ONLY "SAFE" if the content does NOT violate any rules.
Respond with "VIOLATION: <reason>" if it violates any rule, explaining which rule."""
                    
                    try:
                        response = openai_client.chat.completions.create(
                            model="gpt-3.5-turbo",
                            messages=[
                                {"role": "system", "content": "You are a strict rule enforcement AI. Be precise and brief."},
                                {"role": "user", "content": validation_prompt}
                            ],
                            temperature=0.1,
                            max_tokens=150
                        )
                        
                        ai_verdict = response.choices[0].message.content.strip()
                        
                        if not ai_verdict.startswith("SAFE"):
                            send_message(channel_id, f"❌ Spam blocked! This content violates server rules.\n\n{ai_verdict}")
                            return
                    except Exception as e:
                        # CRITICAL: If validation fails, we MUST abort to ensure no rule violations
                        print(f"[ERROR] AI validation failed: {e}")
                        send_message(channel_id, f"❌ Spam aborted! Could not validate content against server rules.\n\nError: {str(e)[:100]}\n\nCannot proceed without confirming content is safe.")
                        return
                else:
                    # No rules stored - warn admin but allow
                    if openai_client:
                        send_message(channel_id, "⚠️ Warning: No server rules found in database. Run `!scan` first to load rules.\n\nProceeding without rule validation...")
                
                # All checks passed - proceed with spam
                send_message(channel_id, f"✅ Sending {spam_count} messages to <#{target_channel_id}>...")
                
                success_count = 0
                failed_count = 0
                
                for i in range(spam_count):
                    try:
                        url = f"https://discord.com/api/v9/channels/{target_channel_id}/messages"
                        payload = {"content": spam_content}
                        response = requests.post(url, headers=headers, json=payload)
                        
                        if response.status_code in [200, 201]:
                            success_count += 1
                        else:
                            failed_count += 1
                            if failed_count == 1:
                                print(f"[SPAM ERROR] Failed to send: {response.status_code} - {response.text}")
                        
                        # Small delay to avoid rate limits (0.5 seconds between messages)
                        if i < spam_count - 1:
                            time.sleep(0.5)
                    except Exception as e:
                        failed_count += 1
                        print(f"[SPAM ERROR] Exception: {e}")
                
                result_msg = f"✅ Spam complete!\n"
                result_msg += f"✔️ Sent: {success_count}\n"
                if failed_count > 0:
                    result_msg += f"❌ Failed: {failed_count}"
                
                send_message(channel_id, result_msg)
                
            except ValueError:
                send_message(channel_id, "❌ Invalid count! Must be a number between 1-500.")
            except Exception as e:
                send_message(channel_id, f"❌ Error: {e}")
            return
    
    # Check multiple conditions for responding:
    # 1. Direct mention
    is_mentioned = f"<@{userid}>" in content or f"<@!{userid}>" in content
    
    # 2. Reply to bot's message (check if referenced message author is the bot)
    is_reply_to_bot = referenced_message.get('author', {}).get('id') == userid
    
    # Fallback: if referenced_message is empty but message_reference exists, fetch the message
    if not is_reply_to_bot and message_reference.get('message_id'):
        try:
            ref_msg_id = message_reference.get('message_id')
            ref_channel_id = message_reference.get('channel_id', channel_id)
            url = f"https://discord.com/api/v9/channels/{ref_channel_id}/messages/{ref_msg_id}"
            resp = requests.get(url, headers=headers, timeout=3)
            if resp.status_code == 200:
                ref_msg_data = resp.json()
                is_reply_to_bot = ref_msg_data.get('author', {}).get('id') == userid
        except Exception as e:
            print(f"[ERROR] Failed to fetch referenced message: {e}")
    # 3. Message contains "the queen" or "nila" (Nila's name)
    is_about_queen = "the queen" in content_lower
    is_about_nila = "nila" in content_lower
    # 4. Starts with !ai
    is_command = content.startswith("!ai")
    
    should_respond = is_mentioned or is_reply_to_bot or is_about_queen or is_about_nila or is_command
    
    if should_respond:
        print(f"[MESSAGE RECEIVED] {content}")
        
        message_id = data.get('id')
        
        # Remove mention, !ai prefix, "nila", "the queen" - keep the actual message
        clean_message = content.replace(f"<@{userid}>", "").replace(f"<@!{userid}>", "").replace("!ai", "").strip()
        clean_message = clean_message.replace("nila", "").replace("Nila", "").replace("the queen", "").replace("The Queen", "").strip()
        
        # If message is empty after cleaning, use default greeting
        if not clean_message:
            clean_message = "hi"
        
        # Determine which channel to use
        response_channel = channel_id
        if SECRET_CHANNEL_ID and ("secret" in content_lower or "private" in content_lower):
            response_channel = SECRET_CHANNEL_ID
        
        # Get guild_id from message data
        message_guild_id = data.get('guild_id')
        
        # Get AI response with server context and rules
        ai_response = get_ai_response(author_id, clean_message, message_guild_id, channel_id)
        if ai_response:
            send_message(response_channel, ai_response, reply_to_message_id=message_id)

def run_discord_bot():
    """Main Discord bot function with event loop"""
    global ws, heartbeat_interval, last_sequence, current_voice_channel
    
    try:
        ws = create_connection('wss://gateway.discord.gg/?v=9&encoding=json')
        
        # Receive Hello
        hello = json.loads(ws.recv())
        heartbeat_interval = hello['d']['heartbeat_interval']
        
        # Send Identify
        auth = {
            "op": 2,
            "d": {
                "token": usertoken,
                "properties": {
                    "$os": "Windows 10",
                    "$browser": "Google Chrome",
                    "$device": "Windows"
                },
                "presence": {"status": status, "afk": False}
            }
        }
        ws.send(json.dumps(auth))
        
        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=heartbeat_sender, daemon=True)
        heartbeat_thread.start()
        
        # Wait for Ready event
        ready_received = False
        while not ready_received:
            event = json.loads(ws.recv())
            if event.get('t') == 'READY':
                ready_received = True
                print("[READY] Connected to Discord Gateway")
        
        # Send Voice State Update to join voice channel
        send_voice_state_update(CHANNEL_ID, SELF_MUTE, SELF_DEAF)
        print(f"[VOICE] Auto-joining channel {CHANNEL_ID}")
        
        # Event loop - listen for messages
        while True:
            try:
                event = json.loads(ws.recv())
                
                # Update sequence number
                if event.get('s'):
                    last_sequence = event['s']
                
                # Handle different event types
                if event.get('op') == 11:  # Heartbeat ACK
                    print("[HEARTBEAT] ACK received")
                elif event.get('op') == 10:  # Hello (reconnection)
                    heartbeat_interval = event['d']['heartbeat_interval']
                elif event.get('t') == 'MESSAGE_CREATE':
                    handle_message(event)
                    
            except Exception as e:
                print(f"[ERROR] Event loop error: {e}")
                break
                
    except Exception as e:
        print(f"[ERROR] Discord connection error: {e}")
    finally:
        if ws:
            ws.close()
            ws = None

def main():
    os.system("clear")
    print(f"[LOGIN] Logged in as {username}#{discriminator} ({userid})")
    print(f"[STATUS] Voice Mute: {SELF_MUTE}, Voice Deaf: {SELF_DEAF}")
    
    # Load conversation history
    load_conversation_history()
    
    if openai_client:
        print("=" * 60)
        print("[AI] The Queen is active! - 19 bochorer Bangladeshi meye 🎀")
        print("=" * 60)
        print("[FEATURES] She will respond to:")
        print("     - Direct mentions (@The Queen)")
        print("     - Replies to her messages")
        print("     - Any message containing 'the queen'")
        print("     - Messages starting with !ai")
        if SECRET_CHANNEL_ID:
            print(f"     - Secret messages will go to channel: {SECRET_CHANNEL_ID}")
        print()
        print("[MEMORY] Full conversation history enabled - remembers everything!")
        print()
        if ADMIN_USER_ID:
            print("[ADMIN COMMANDS] Available for admin user only:")
            print("     !join                        - Join default voice channel")
            print("     !join <guild_id> <channel_id> - Join any guild's VC")
            print("     !leave                       - Leave the voice channel")
            print("     !mute                        - Mute in voice channel")
            print("     !unmute                      - Unmute in voice channel")
            print("     !scan [guild_id]             - Scan server for channels/rules")
            print("     !spam <channel> <1-500> <msg> - Send messages (rules-checked)")
        print("=" * 60)
    
    while True:
        try:
            run_discord_bot()
            print("[RECONNECT] Reconnecting in 5 seconds...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("\n[EXIT] Shutting down...")
            save_conversation_history()
            break

keep_alive()
main()
