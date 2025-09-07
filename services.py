from agents import function_tool
from datetime import datetime
import os
from pymongo import MongoClient
from dotenv import load_dotenv
import json

load_dotenv()


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


@function_tool
def get_current_time() -> str:
    """Get the current time."""
    return f"The time right now is {datetime.now().strftime('%H:%M:%S')}."


@function_tool
def insert_into_mongodb(first_name: str, last_name: str, feedback: str) -> str:
    """Insert user feedback into the MongoDB database."""
    try:
        mongo_uri = os.getenv("CONNECTION_STRING")
        if not mongo_uri:
            return "CONNECTION_STRING not found in .env file."

        client = MongoClient(mongo_uri)
        db = client["feedback_db"]
        collection = db["user_feedback"]
        
        data_to_insert = {
            "first_name": first_name,
            "last_name": last_name,
            "feedback": feedback,
            "timestamp": datetime.now()
        }
        
        result = collection.insert_one(data_to_insert)
        client.close()
        return f"Successfully inserted document with id: {result.inserted_id}"
    except Exception as e:
        return f"An error occurred: {e}"


