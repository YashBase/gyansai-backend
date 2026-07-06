from server import app

__all__ = ["app"]


@app.get("/")
async def home():
    return {
        "status": "success",
        "message": "FastAPI backend is running successfully!"
    }
