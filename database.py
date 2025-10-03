import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import threading

class DiscordBotDB:
    """Database manager for Discord bot with server rules and context awareness"""
    
    def __init__(self, db_path="discord_bot.db"):
        self.db_path = db_path
        self.local = threading.local()
        self.init_database()
    
    def get_connection(self):
        """Get thread-local database connection"""
        if not hasattr(self.local, 'conn'):
            self.local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.local.conn.row_factory = sqlite3.Row
        return self.local.conn
    
    def init_database(self):
        """Initialize database schema"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Servers/Guilds table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS servers (
                guild_id TEXT PRIMARY KEY,
                guild_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rules_channel_id TEXT,
                last_rules_update TIMESTAMP,
                metadata TEXT
            )
        ''')
        
        # Channels table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                guild_id TEXT,
                channel_name TEXT,
                channel_type TEXT,
                topic TEXT,
                is_rules_channel BOOLEAN DEFAULT 0,
                last_scanned TIMESTAMP,
                FOREIGN KEY (guild_id) REFERENCES servers(guild_id)
            )
        ''')
        
        # Server rules table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS server_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                rule_text TEXT,
                rule_category TEXT,
                source_channel_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (guild_id) REFERENCES servers(guild_id)
            )
        ''')
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                preferences TEXT
            )
        ''')
        
        # Conversation history table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS conversation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                guild_id TEXT,
                channel_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (guild_id) REFERENCES servers(guild_id)
            )
        ''')
        
        # Create indexes for better performance
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversation_user 
            ON conversation_history(user_id, timestamp DESC)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_conversation_guild 
            ON conversation_history(guild_id, timestamp DESC)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_rules_guild 
            ON server_rules(guild_id)
        ''')
        
        conn.commit()
        print("[DATABASE] Initialized successfully")
    
    # Server/Guild management
    def add_or_update_server(self, guild_id: str, guild_name: str, metadata: dict = None):
        """Add or update server information"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        metadata_json = json.dumps(metadata) if metadata else None
        
        cursor.execute('''
            INSERT INTO servers (guild_id, guild_name, metadata)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                guild_name = excluded.guild_name,
                metadata = excluded.metadata
        ''', (guild_id, guild_name, metadata_json))
        
        conn.commit()
        print(f"[DATABASE] Added/updated server: {guild_name} ({guild_id})")
    
    def get_server(self, guild_id: str) -> Optional[Dict]:
        """Get server information"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM servers WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        return None
    
    # Channel management
    def add_or_update_channel(self, channel_id: str, guild_id: str, channel_name: str, 
                               channel_type: str = "text", topic: str = None, 
                               is_rules_channel: bool = False):
        """Add or update channel information"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO channels (channel_id, guild_id, channel_name, channel_type, topic, is_rules_channel, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                channel_name = excluded.channel_name,
                channel_type = excluded.channel_type,
                topic = excluded.topic,
                is_rules_channel = excluded.is_rules_channel,
                last_scanned = CURRENT_TIMESTAMP
        ''', (channel_id, guild_id, channel_name, channel_type, topic, int(is_rules_channel)))
        
        conn.commit()
    
    def get_channels(self, guild_id: str) -> List[Dict]:
        """Get all channels for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM channels WHERE guild_id = ? ORDER BY channel_name', (guild_id,))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_rules_channel(self, guild_id: str) -> Optional[Dict]:
        """Get the rules channel for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM channels 
            WHERE guild_id = ? AND is_rules_channel = 1 
            LIMIT 1
        ''', (guild_id,))
        
        row = cursor.fetchone()
        return dict(row) if row else None
    
    # Server rules management
    def add_server_rule(self, guild_id: str, rule_text: str, rule_category: str = "general", 
                        source_channel_id: str = None):
        """Add a server rule"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO server_rules (guild_id, rule_text, rule_category, source_channel_id)
            VALUES (?, ?, ?, ?)
        ''', (guild_id, rule_text, rule_category, source_channel_id))
        
        conn.commit()
    
    def get_server_rules(self, guild_id: str) -> List[Dict]:
        """Get all rules for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM server_rules 
            WHERE guild_id = ? 
            ORDER BY id
        ''', (guild_id,))
        
        return [dict(row) for row in cursor.fetchall()]
    
    def clear_server_rules(self, guild_id: str):
        """Clear all rules for a server (useful before re-scanning)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM server_rules WHERE guild_id = ?', (guild_id,))
        conn.commit()
    
    def update_rules_channel(self, guild_id: str, rules_channel_id: str):
        """Update the rules channel ID for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE servers 
            SET rules_channel_id = ?, last_rules_update = CURRENT_TIMESTAMP
            WHERE guild_id = ?
        ''', (rules_channel_id, guild_id))
        
        conn.commit()
    
    # User management
    def add_or_update_user(self, user_id: str, username: str, preferences: dict = None):
        """Add or update user information"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        preferences_json = json.dumps(preferences) if preferences else None
        
        cursor.execute('''
            INSERT INTO users (user_id, username, preferences, last_seen)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                preferences = excluded.preferences,
                last_seen = CURRENT_TIMESTAMP
        ''', (user_id, username, preferences_json))
        
        conn.commit()
    
    def get_user(self, user_id: str) -> Optional[Dict]:
        """Get user information"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        return None
    
    # Conversation history management
    def add_conversation_message(self, user_id: str, role: str, content: str, 
                                  guild_id: str = None, channel_id: str = None):
        """Add a message to conversation history"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO conversation_history (user_id, guild_id, channel_id, role, content)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, guild_id, channel_id, role, content))
        
        conn.commit()
    
    def get_conversation_history(self, user_id: str, guild_id: str = None, limit: int = 20) -> List[Dict]:
        """Get conversation history for a user, optionally filtered by guild
        Returns list of dicts with 'role' and 'content' keys, compatible with OpenAI API"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if guild_id:
            cursor.execute('''
                SELECT role, content 
                FROM conversation_history 
                WHERE user_id = ? AND guild_id = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (user_id, guild_id, limit))
        else:
            cursor.execute('''
                SELECT role, content 
                FROM conversation_history 
                WHERE user_id = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (user_id, limit))
        
        # Convert to list of dicts with only role and content (OpenAI format)
        messages = [{"role": row["role"], "content": row["content"]} for row in cursor.fetchall()]
        messages.reverse()  # Return in chronological order
        return messages
    
    def cleanup_old_conversations(self, user_id: str, keep_last: int = 100):
        """Clean up old conversation history for a user, keeping only the most recent messages"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM conversation_history 
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM conversation_history 
                WHERE user_id = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            )
        ''', (user_id, user_id, keep_last))
        
        conn.commit()
    
    # Import from old JSON format
    def import_from_json(self, json_file_path: str):
        """Import conversation history from old conversation_memory.json format"""
        try:
            with open(json_file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            conn = self.get_connection()
            cursor = conn.cursor()
            
            imported_count = 0
            for user_id, messages in data.items():
                # Add user if not exists
                cursor.execute('''
                    INSERT OR IGNORE INTO users (user_id, username)
                    VALUES (?, ?)
                ''', (user_id, f"user_{user_id}"))
                
                # Add messages
                for msg in messages:
                    cursor.execute('''
                        INSERT INTO conversation_history (user_id, role, content)
                        VALUES (?, ?, ?)
                    ''', (user_id, msg.get('role'), msg.get('content')))
                    imported_count += 1
            
            conn.commit()
            print(f"[DATABASE] Imported {imported_count} messages from {json_file_path}")
            return True
        except Exception as e:
            print(f"[DATABASE ERROR] Failed to import from JSON: {e}")
            return False
    
    def get_context_for_ai(self, user_id: str, guild_id: str = None) -> Dict:
        """Get comprehensive context for AI response including user history and server rules"""
        context = {
            "user_history": [],
            "server_rules": [],
            "server_info": None,
            "channels": []
        }
        
        # Get conversation history
        history = self.get_conversation_history(user_id, guild_id, limit=10)
        context["user_history"] = [{"role": msg["role"], "content": msg["content"]} for msg in history]
        
        # Get server info and rules if guild_id provided
        if guild_id:
            server = self.get_server(guild_id)
            if server:
                context["server_info"] = server
                
                # Get server rules
                rules = self.get_server_rules(guild_id)
                context["server_rules"] = [rule["rule_text"] for rule in rules]
                
                # Get channels (useful for context)
                channels = self.get_channels(guild_id)
                context["channels"] = [{"name": ch["channel_name"], "topic": ch["topic"]} for ch in channels if ch["topic"]]
        
        return context
    
    def close(self):
        """Close database connection"""
        if hasattr(self.local, 'conn'):
            self.local.conn.close()
