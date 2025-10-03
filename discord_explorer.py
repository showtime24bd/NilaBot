import requests
import re
from typing import List, Dict, Optional
import time

class DiscordServerExplorer:
    """Explore Discord servers to find channels, rules, and context"""
    
    def __init__(self, token: str, db):
        self.token = token
        self.db = db
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json"
        }
    
    def explore_server(self, guild_id: str) -> Dict:
        """
        Explore a Discord server - get all channels, find rules, scan topics
        Returns: dict with server info, channels, and rules
        """
        result = {
            "success": False,
            "guild_info": None,
            "channels": [],
            "rules_found": [],
            "error": None
        }
        
        try:
            # Get guild info
            guild_info = self.get_guild_info(guild_id)
            if not guild_info:
                result["error"] = "Failed to fetch guild info"
                return result
            
            result["guild_info"] = guild_info
            
            # Get all channels
            channels = self.get_guild_channels(guild_id)
            if not channels:
                result["error"] = "Failed to fetch channels"
                return result
            
            result["channels"] = channels
            
            # Store server in database
            self.db.add_or_update_server(
                guild_id=guild_id,
                guild_name=guild_info.get("name", "Unknown Server"),
                metadata={"icon": guild_info.get("icon")}
            )
            
            # Process and store each channel
            rules_channel = None
            for channel in channels:
                channel_id = channel.get("id")
                channel_name = channel.get("name", "")
                channel_type = channel.get("type", 0)
                topic = channel.get("topic", "")
                
                # Check if this is a rules channel
                is_rules = self.is_rules_channel(channel_name, topic)
                
                if channel_type == 0:  # Text channel
                    self.db.add_or_update_channel(
                        channel_id=channel_id,
                        guild_id=guild_id,
                        channel_name=channel_name,
                        channel_type="text",
                        topic=topic,
                        is_rules_channel=is_rules
                    )
                    
                    if is_rules:
                        rules_channel = channel
            
            # If we found a rules channel, try to read the rules
            if rules_channel:
                print(f"[EXPLORER] Found rules channel: #{rules_channel.get('name')}")
                rules = self.read_rules_from_channel(guild_id, rules_channel.get('id'))
                result["rules_found"] = rules
                
                # Store rules in database
                self.db.clear_server_rules(guild_id)  # Clear old rules
                for rule in rules:
                    self.db.add_server_rule(
                        guild_id=guild_id,
                        rule_text=rule,
                        rule_category="server_rules",
                        source_channel_id=rules_channel.get('id')
                    )
                
                self.db.update_rules_channel(guild_id, rules_channel.get('id'))
                
                print(f"[EXPLORER] Stored {len(rules)} rules for server {guild_info.get('name')}")
            else:
                print(f"[EXPLORER] No rules channel found for server {guild_info.get('name')}")
            
            result["success"] = True
            return result
            
        except Exception as e:
            result["error"] = str(e)
            print(f"[EXPLORER ERROR] Failed to explore server: {e}")
            return result
    
    def get_guild_info(self, guild_id: str) -> Optional[Dict]:
        """Get basic guild/server information"""
        try:
            url = f"https://discord.com/api/v9/guilds/{guild_id}"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"[EXPLORER] Failed to get guild info: {response.status_code}")
                return None
        except Exception as e:
            print(f"[EXPLORER ERROR] Failed to get guild info: {e}")
            return None
    
    def get_guild_channels(self, guild_id: str) -> List[Dict]:
        """Get all channels in a guild"""
        try:
            url = f"https://discord.com/api/v9/guilds/{guild_id}/channels"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                channels = response.json()
                print(f"[EXPLORER] Found {len(channels)} channels")
                return channels
            else:
                print(f"[EXPLORER] Failed to get channels: {response.status_code}")
                return []
        except Exception as e:
            print(f"[EXPLORER ERROR] Failed to get channels: {e}")
            return []
    
    def is_rules_channel(self, channel_name: str, topic: str = "") -> bool:
        """Check if a channel is likely a rules channel based on name and topic"""
        rules_keywords = [
            "rule", "rules", "guideline", "guidelines", "regulation", 
            "নিয়ম", "বিধি", "রেগুলেশন", "গাইডলাইন"
        ]
        
        channel_name_lower = channel_name.lower()
        topic_lower = topic.lower() if topic else ""
        
        # Check channel name
        for keyword in rules_keywords:
            if keyword in channel_name_lower or keyword in topic_lower:
                return True
        
        return False
    
    def read_rules_from_channel(self, guild_id: str, channel_id: str, limit: int = 50) -> List[str]:
        """
        Read rules from a channel by fetching messages
        Returns: List of rule texts
        """
        rules = []
        
        try:
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit={limit}"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code != 200:
                print(f"[EXPLORER] Failed to read messages: {response.status_code}")
                return rules
            
            messages = response.json()
            print(f"[EXPLORER] Reading {len(messages)} messages from rules channel")
            
            # Process messages to extract rules
            for msg in reversed(messages):  # Read from oldest to newest
                content = msg.get("content", "")
                embeds = msg.get("embeds", [])
                
                # Extract rules from message content
                extracted = self.extract_rules_from_text(content)
                rules.extend(extracted)
                
                # Extract rules from embeds
                for embed in embeds:
                    embed_desc = embed.get("description", "")
                    embed_title = embed.get("title", "")
                    
                    extracted = self.extract_rules_from_text(f"{embed_title}\n{embed_desc}")
                    rules.extend(extracted)
            
            # Remove duplicates while preserving order
            seen = set()
            unique_rules = []
            for rule in rules:
                if rule not in seen and len(rule) > 10:  # Ignore very short rules
                    seen.add(rule)
                    unique_rules.append(rule)
            
            return unique_rules
            
        except Exception as e:
            print(f"[EXPLORER ERROR] Failed to read rules: {e}")
            return rules
    
    def extract_rules_from_text(self, text: str) -> List[str]:
        """
        Extract individual rules from text
        Looks for numbered lists, bullet points, etc.
        Handles Discord custom emojis and markdown formatting
        """
        if not text or len(text.strip()) < 10:
            return []
        
        # Clean Discord formatting
        # Remove custom emojis: <a:NAME:ID> or <:NAME:ID>
        text = re.sub(r'<a?:[^:]+:\d+>', '', text)
        # Remove markdown bold/italic markers
        text = text.replace('**', '').replace('*', '')
        # Remove excessive whitespace
        text = re.sub(r'\n\s*\n', '\n\n', text)
        
        rules = []
        
        # Pattern 1: Discord custom emoji bullets followed by text (Enhanced for Bengali servers)
        # Example: <a:AG_Dot:ID> **No Spam** <a:AG_Arrow:ID> Bengali text
        # Also handles: <:emoji:ID> text OR <a:emoji:ID> text
        emoji_bullet_pattern = r'(?:^|\n)\s*<a?:[^>]+>\s*(?:\*\*)?([^*\n<]+?)(?:\*\*)?\s*(?:<a?:[^>]+>)?\s*([^\n<]+?)(?=\n\s*<a?:|$)'
        matches = re.findall(emoji_bullet_pattern, text, re.MULTILINE | re.DOTALL)
        for title, description in matches:
            # Clean up both parts
            title_clean = title.strip()
            desc_clean = description.strip()
            
            # If description is substantial, combine; otherwise just use title
            if desc_clean and len(desc_clean) > 10:
                combined = f"{title_clean} - {desc_clean}"
            else:
                combined = title_clean
            
            if len(combined) > 15:
                rules.append(combined)
        
        # Pattern 2: Numbered rules (1., 2., etc. or 1), 2), etc.)
        if not rules:
            numbered_pattern = r'(?:^|\n)\s*(\d+[\.\)])\s*(.+?)(?=\n\s*\d+[\.\)]|\n\n|$)'
            matches = re.findall(numbered_pattern, text, re.MULTILINE | re.DOTALL)
            for num, rule_text in matches:
                cleaned = rule_text.strip()
                if len(cleaned) > 10:
                    rules.append(cleaned)
        
        # Pattern 3: Bullet points (-, *, •, ➤, etc.)
        if not rules:
            bullet_pattern = r'(?:^|\n)\s*[\-\*\•\➤]\s*(.+?)(?=\n\s*[\-\*\•\➤]|\n\n|$)'
            matches = re.findall(bullet_pattern, text, re.MULTILINE | re.DOTALL)
            for rule_text in matches:
                cleaned = rule_text.strip()
                if len(cleaned) > 10:
                    rules.append(cleaned)
        
        # Pattern 4: "Rule 1:", "নিয়ম ১:", etc.
        if not rules:
            rule_label_pattern = r'(?:^|\n)\s*(?:Rule|নিয়ম)\s*\d+\s*[:：]\s*(.+?)(?=\n\s*(?:Rule|নিয়ম)\s*\d+|$)'
            matches = re.findall(rule_label_pattern, text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
            for rule_text in matches:
                cleaned = rule_text.strip()
                if len(cleaned) > 10:
                    rules.append(cleaned)
        
        # Pattern 5: Split by double newlines if nothing else worked
        if not rules and len(text.strip()) > 20:
            paragraphs = [p.strip() for p in text.split('\n\n') if len(p.strip()) > 20]
            rules.extend(paragraphs)
        
        # Final cleanup: remove emoji codes, extra spaces
        cleaned_rules = []
        for rule in rules:
            # Remove any remaining emoji codes
            clean = re.sub(r'<a?:[^>]+>', '', rule)
            # Remove extra whitespace
            clean = ' '.join(clean.split())
            if len(clean) > 15:
                cleaned_rules.append(clean)
        
        return cleaned_rules
    
    def get_channel_info(self, channel_id: str) -> Optional[Dict]:
        """Get information about a specific channel"""
        try:
            url = f"https://discord.com/api/v9/channels/{channel_id}"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                return response.json()
            else:
                return None
        except Exception as e:
            print(f"[EXPLORER ERROR] Failed to get channel info: {e}")
            return None
    
    def scan_channel_for_context(self, channel_id: str, message_limit: int = 20) -> List[str]:
        """
        Scan a channel to understand its purpose and context
        Returns: List of context messages/topics
        """
        context = []
        
        try:
            # Get channel info first
            channel_info = self.get_channel_info(channel_id)
            if channel_info and channel_info.get("topic"):
                context.append(f"Channel topic: {channel_info.get('topic')}")
            
            # Get recent messages
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit={message_limit}"
            response = requests.get(url, headers=self.headers, timeout=5)
            
            if response.status_code == 200:
                messages = response.json()
                
                # Analyze message patterns
                total_msgs = len(messages)
                if total_msgs > 0:
                    context.append(f"Recent activity: {total_msgs} messages")
                
                return context
            
            return context
            
        except Exception as e:
            print(f"[EXPLORER ERROR] Failed to scan channel: {e}")
            return context
