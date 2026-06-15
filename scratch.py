import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://tala:tala@localhost:5432/tala')
    await conn.execute("TRUNCATE TABLE tasks CASCADE;")
    await conn.execute("TRUNCATE TABLE goal_decompositions CASCADE;")
    print("Database cleared successfully!")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
