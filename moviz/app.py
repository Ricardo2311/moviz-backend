from fastapi import FastAPI

app = FastAPI(title="Moviz Movie Ratings Service")


@app.get("/")
def read_root():
    return {"message": "Welcome to Moviz API"}
