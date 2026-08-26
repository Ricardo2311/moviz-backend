import asyncio
import time
import httpx

BASE = "http://127.0.0.1:8000"

async def wait_up(client, timeout=15):
    for _ in range(timeout):
        try:
            r = await client.get(f"{BASE}/openapi.json", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            await asyncio.sleep(1)
    return False

async def main():
    async with httpx.AsyncClient() as client:
        ready = await wait_up(client, timeout=20)
        if not ready:
            print("Server did not become ready")
            return

        # try register (ignore failure if exists)
        reg_data = {"username": "testuser", "email": "test@example.com", "password": "secret"}
        try:
            r = await client.post(f"{BASE}/auth/register", json=reg_data, timeout=10)
            print("register status:", r.status_code)
            try:
                print(r.json())
            except Exception:
                pass
        except Exception as e:
            print("register error:", e)

        # login
        login_data = {"username": "testuser", "password": "secret"}
        r = await client.post(f"{BASE}/auth/login", json=login_data, timeout=10)
        print("login status:", r.status_code)
        tok = None
        try:
            j = r.json()
            tok = j.get("access_token")
            print("token:", tok)
        except Exception as e:
            print("login json error:", e)
            return

        # movies
        headers = {"Authorization": f"Bearer {tok}"}
        r = await client.get(f"{BASE}/movies", headers=headers, timeout=10)
        print("movies status:", r.status_code)
        print("movies body:")
        try:
            print(r.text)
        except Exception as e:
            print("movies text error:", e)


if __name__ == '__main__':
    asyncio.run(main())
