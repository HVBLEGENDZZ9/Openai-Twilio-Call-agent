from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()
CONNECTION_STRING = os.getenv("CONNECTION_STRING")
def get_database(database_name='feedback_db'):
 
   # Provide the mongodb atlas url to connect python to mongodb using pymongo
   client = MongoClient(CONNECTION_STRING)
   # Create a connection using MongoClient. You can import MongoClient or use pymongo.MongoClient
 
   # Create the database for our example (we will use the same database throughout the tutorial
   return client[database_name]

# This is added so that many files can reuse the function get_database()
if __name__ == "__main__":   
  
   # Get the database
   db = get_database()
   collection = db["feedback_collection"]
   feedback = {"firstName": "John Doe", "lastName": "Doe", "feedback": "Great service!"}
   collection.insert_one(feedback)

   

