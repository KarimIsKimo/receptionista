from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

app = FastAPI()

VERIFY_TOKEN = "Neckface@2003" 

# 1. GET request for Meta's initial verification
@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("WEBHOOK_VERIFIED")
            # Changed to PlainTextResponse so Meta doesn't reject it
            return PlainTextResponse(content=challenge)
        else:
            return Response(content="Verification failed", status_code=403)
            
    return Response(content="Bad Request", status_code=400)

# 2. POST request to receive patient messages
@app.post("/webhook")
async def receive_message(request: Request):
    # This captures the incoming message payload from Meta
    body = await request.json()
    
    # Print it to your Render logs so you can see what patients are sending
    print("Incoming Webhook Data:", body)
    
    # You MUST return a 200 OK fast, otherwise Meta thinks your server is down
    return Response(content="EVENT_RECEIVED", status_code=200)
