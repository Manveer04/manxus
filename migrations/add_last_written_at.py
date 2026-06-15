"""
Database migration: Add last_written_at column to platform_listings
"""
from sqlalchemy import create_engine, MetaData, Table, Column, DateTime

db_path = "z:/inventory-sync/db/inventory.db"
engine = create_engine(f"sqlite:///{db_path}")
metadata = MetaData()
metadata.reflect(bind=engine)

platform_listings = Table("platform_listings", metadata, autoload_with=engine)

# Only add if not exists
def column_exists(table, column):
    return column in [c.name for c in table.columns]

def upgrade():
    with engine.connect() as conn:
        if not column_exists(platform_listings, "last_written_at"):
            conn.execute('ALTER TABLE platform_listings ADD COLUMN last_written_at DATETIME NULL')
            print("Added last_written_at column to platform_listings.")
        else:
            print("last_written_at column already exists.")

if __name__ == "__main__":
    upgrade()
