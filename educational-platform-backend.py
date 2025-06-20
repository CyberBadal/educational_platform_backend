import os
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional
import motor.motor_asyncio
from bson import ObjectId
import uvicorn

# MongoDB connection
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client.educational_platform

app = FastAPI(title="Educational Platform Backend")

# Helper for ObjectId serialization and validation
class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid ObjectId")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, *args, **kwargs):
        return {"type": "string"}

# User Model
class User(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    username: str
    is_instructor: bool = False

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}

# Quiz Question Model
class QuizQuestion(BaseModel):
    question: str
    options: List[str]
    correct_option_index: int

# Course Model
class Course(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    title: str
    description: Optional[str] = None
    instructor_id: PyObjectId
    quizzes: List[QuizQuestion] = []

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}

# Progress Model
class Progress(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    student_id: PyObjectId
    course_id: PyObjectId
    completed_quizzes: List[int] = []
    assignments_completed: bool = False

    class Config:
        allow_population_by_field_name = True
        json_encoders = {ObjectId: str}

# Request models for API endpoints
class UserCreateRequest(BaseModel):
    username: str
    is_instructor: Optional[bool] = False

class CourseCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    instructor_username: str

class EnrollRequest(BaseModel):
    student_username: str

class CompleteQuizRequest(BaseModel):
    quiz_index: int

# --- API Endpoints ---

# Register user
@app.post("/users/", response_model=User)
async def create_user(user: UserCreateRequest):
    existing = await db.users.find_one({"username": user.username})
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    user_doc = {"username": user.username, "is_instructor": user.is_instructor}
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = result.inserted_id
    return User(**user_doc)

# Create course (Instructor only)
@app.post("/courses/", response_model=Course)
async def create_course(course: CourseCreateRequest):
    instructor = await db.users.find_one({"username": course.instructor_username, "is_instructor": True})
    if not instructor:
        raise HTTPException(status_code=403, detail="Only instructors can create courses")
    course_doc = {
        "title": course.title,
        "description": course.description,
        "instructor_id": instructor["_id"],
        "quizzes": []
    }
    result = await db.courses.insert_one(course_doc)
    course_doc["_id"] = result.inserted_id
    return Course(**course_doc)

# Add quiz question to course
@app.post("/courses/{course_id}/quizzes/", response_model=Course)
async def add_quiz(course_id: str, quiz: QuizQuestion):
    course_oid = PyObjectId.validate(course_id)
    course = await db.courses.find_one({"_id": course_oid})
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    await db.courses.update_one(
        {"_id": course_oid},
        {"$push": {"quizzes": quiz.dict()}}
    )
    updated = await db.courses.find_one({"_id": course_oid})
    return Course(**updated)

# List all courses
@app.get("/courses/", response_model=List[Course])
async def list_courses():
    courses = []
    cursor = db.courses.find()
    async for course in cursor:
        courses.append(Course(**course))
    return courses

# Enroll student in course (creates progress if not exists)
@app.post("/courses/{course_id}/enroll/", response_model=Progress)
async def enroll_student(course_id: str, enroll_req: EnrollRequest):
    student = await db.users.find_one({"username": enroll_req.student_username, "is_instructor": False})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    course_oid = PyObjectId.validate(course_id)
    progress = await db.progress.find_one({"student_id": student["_id"], "course_id": course_oid})
    if progress:
        return Progress(**progress)
    prog_doc = {
        "student_id": student["_id"],
        "course_id": course_oid,
        "completed_quizzes": [],
        "assignments_completed": False
    }
    result = await db.progress.insert_one(prog_doc)
    prog_doc["_id"] = result.inserted_id
    return Progress(**prog_doc)

# Get student progress for course
@app.get("/progress/{course_id}/{student_username}/", response_model=Progress)
async def get_progress(course_id: str, student_username: str):
    student = await db.users.find_one({"username": student_username, "is_instructor": False})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    prog = await db.progress.find_one({"student_id": student["_id"], "course_id": PyObjectId.validate(course_id)})
    if not prog:
        raise HTTPException(status_code=404, detail="Progress not found")
    return Progress(**prog)

# Mark quiz complete
@app.post("/progress/{course_id}/{student_username}/complete_quiz/")
async def complete_quiz(course_id: str, student_username: str, complete_quiz_req: CompleteQuizRequest):
    student = await db.users.find_one({"username": student_username, "is_instructor": False})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    prog = await db.progress.find_one({"student_id": student["_id"], "course_id": PyObjectId.validate(course_id)})
    if not prog:
        raise HTTPException(status_code=404, detail="Progress not found")
    quizzes_done = prog.get("completed_quizzes", [])
    if complete_quiz_req.quiz_index not in quizzes_done:
        quizzes_done.append(complete_quiz_req.quiz_index)
        await db.progress.update_one({"_id": prog["_id"]}, {"$set": {"completed_quizzes": quizzes_done}})
    return {"message": "Quiz marked complete"}

# Mark assignment complete
@app.post("/progress/{course_id}/{student_username}/complete_assignment/")
async def complete_assignment(course_id: str, student_username: str):
    student = await db.users.find_one({"username": student_username, "is_instructor": False})
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    prog = await db.progress.find_one({"student_id": student["_id"], "course_id": PyObjectId.validate(course_id)})
    if not prog:
        raise HTTPException(status_code=404, detail="Progress not found")
    await db.progress.update_one({"_id": prog["_id"]}, {"$set": {"assignments_completed": True}})
    return {"message": "Assignment marked complete"}

if __name__ == "__main__":
    uvicorn.run("educational-platform-backend:app", host="0.0.0.0", port=8000, reload=True)

