import requests
import base64

CLIENT_ID = "5a6951c492284724987f84257456b424"
CLIENT_SECRET = "dcacfbc1280e4964a3aa4be6a968a436"

def get_access_token():
    auth = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
    }
    data = {
        "grant_type": "client_credentials"
    }

    res = requests.post("https://accounts.spotify.com/api/token", headers=headers, data=data)
    return res.json().get("access_token")
