import sqlite3
import pandas as pd

# Create a simple database file named 'trades.db'
def init_db():
    conn = sqlite3.connect('trades.db')
    cursor = conn.cursor()
    # Create a table to record every trade if it doesn't exist yet
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT,
            side TEXT,
            quantity REAL,
            price REAL,
            strategy_name TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Function to add a single trade into the database
def add_trade(symbol, side, quantity, price, strategy_name):
    conn = sqlite3.connect('trades.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO trades (symbol, side, quantity, price, strategy_name)
        VALUES (?, ?, ?, ?, ?)
    ''', (symbol, side.upper(), quantity, price, strategy_name))
    conn.commit()
    conn.close()

# Function to read all recorded trades into a clean table
def get_all_trades():
    conn = sqlite3.connect('trades.db')
    df = pd.read_sql_query("SELECT * FROM trades ORDER BY timestamp DESC", conn)
    conn.close()
    return df

# Directly run the initialization when the script executes
init_db()
print("Database initialized successfully!")