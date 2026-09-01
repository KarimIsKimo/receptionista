from fastapi import FastAPI, Request, Response

app = FastAPI()

# You can change this token to anything you want, just remember it for Step 4!
VERIFY_TOKEN = "my_custom_secret_token" 

@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("WEBHOOK_VERIFIED")
            return int(challenge)
        else:
            return Response(content="Verification failed", status_code=403)
            
    return Response(content="Bad Request", status_code=400)
