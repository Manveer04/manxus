import sqlite3
con = sqlite3.connect('z:/inventory-sync/db/inventory.db')
try:
    con.execute('ALTER TABLE platform_listings ADD COLUMN last_written_at DATETIME')
    con.commit()
    print('column added')
except Exception as e:
    print(f'error: {e}')
finally:
    result = con.execute('PRAGMA table_info(platform_listings)').fetchall()
    for row in result:
        print(row)
    con.close()
