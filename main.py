from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prediction import predict_breast_tumor

import os
import uuid
from typing import Optional
from datetime import datetime

from database import supabase, supabase_admin
from auth import get_current_doctor
from prediction import predict_breast_tumor, load_model
from pdf_generator import generate_medical_report

# ------------------------
# FastAPI app
# ------------------------
app = FastAPI(title="Breast Tumor Detection AI (Supabase)")

# ------------------------
# CORS
# ------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------
# Directories (Legacy for local processing, then upload to Supabase)
# ------------------------
os.makedirs("uploads", exist_ok=True)
os.makedirs("reports", exist_ok=True)

# ------------------------
# Serve frontend
# ------------------------
app.mount("/app", StaticFiles(directory="front-end", html=True), name="front-end")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ------------------------
# Load model on startup
# ------------------------
@app.on_event("startup")
async def startup_event():
    load_model()
    print("[OK] Model loaded successfully!")

# ============================================================
# AUTH ENDPOINTS
# ============================================================
@app.post("/signup")
async def signup(
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    license_number: str = Form(...)
):
    try:
        # Sign up with Supabase
        res = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "full_name": full_name,
                    "license_number": license_number
                }
            }
        })
        
        if not res.user:
            raise HTTPException(status_code=400, detail="Registration failed")
        
        return {"message": "Doctor registered successfully. Please check your email for confirmation.", "doctor_id": res.user.id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends()
):
    try:
        res = supabase.auth.sign_in_with_password({
            "email": form_data.username,
            "password": form_data.password
        })
        
        if not res.session:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        user = res.user
        return {
            "access_token": res.session.access_token,
            "token_type": "bearer",
            "doctor": {
                "id": user.id,
                "email": user.email,
                "full_name": user.user_metadata.get("full_name"),
                "license_number": user.user_metadata.get("license_number")
            }
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail="Incorrect email or password")


@app.get("/me")
async def get_current_user(current_doctor: dict = Depends(get_current_doctor)):
    return current_doctor

# ============================================================
# PREDICTION ENDPOINT
# ============================================================
@app.post("/predict")
async def predict_tumor(
    file: UploadFile = File(...),
    patient_name: str = Form(...),
    patient_id: str = Form(...),
    notes: Optional[str] = Form(None),
    current_doctor: dict = Depends(get_current_doctor)
):
    if not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")

    unique_id = str(uuid.uuid4())
    image_filename = f"{unique_id}_original.jpg"
    gradcam_filename = f"{unique_id}_gradcam.jpg"

    local_image_path = os.path.join("uploads", image_filename)
    local_gradcam_path = os.path.join("uploads", gradcam_filename)

    image_bytes = await file.read()

    try:
        # Run prediction
        result = await predict_breast_tumor(image_bytes, local_image_path, local_gradcam_path)
        prediction = result["prediction"]
        confidence = result["confidence"]
        malignant_percentage = result["malignant_percentage"]
        
        # -------------------------------
        # Upload to Supabase Storage
        # -------------------------------
        # Create buckets if they don't exist (only needed once, but safe to call)
        try:
            supabase.storage.create_bucket("scans", options={"public": True})
        except: pass
        
        # Upload original
        with open(local_image_path, "rb") as f:
            supabase.storage.from_("scans").upload(image_filename, f)
        
        # Upload GradCAM
        with open(local_gradcam_path, "rb") as f:
            supabase.storage.from_("scans").upload(gradcam_filename, f)
            
        # Get Public URLs
        image_url = supabase.storage.from_("scans").get_public_url(image_filename)
        gradcam_url = supabase.storage.from_("scans").get_public_url(gradcam_filename)

        # -------------------------------
        # Save scan in Supabase Database
        # -------------------------------
        scan_data = {
            "doctor_id": current_doctor["id"],
            "patient_name": patient_name,
            "patient_id": patient_id,
            "image_path": image_url,
            "gradcam_path": gradcam_url,
            "prediction": prediction,
            "confidence": confidence,
            "malignant_percentage": malignant_percentage,
            "notes": notes,
            "scan_date": datetime.utcnow().isoformat()
        }
        
        db_res = supabase.table("scans").insert(scan_data).execute()
        new_scan = db_res.data[0]

        # -------------------------------
        # Generate PDF report and upload
        # -------------------------------
        local_pdf_path = os.path.join("reports", f"Report_{new_scan['id']}.pdf")
        generate_medical_report({
            "patient_name": patient_name,
            "patient_id": patient_id,
            "scan_date": scan_data["scan_date"],
            "doctor_name": current_doctor["full_name"],
            "prediction": prediction,
            "confidence": confidence,
            "malignant_percentage": malignant_percentage,
            "image_path": local_image_path,
            "gradcam_path": local_gradcam_path,
            "notes": notes
        }, local_pdf_path)
        
        pdf_filename = f"Report_{new_scan['id']}.pdf"
        with open(local_pdf_path, "rb") as f:
            supabase.storage.from_("reports").upload(pdf_filename, f)
            
        pdf_url = supabase.storage.from_("reports").get_public_url(pdf_filename)

        return {
            "scan_id": new_scan["id"],
            "prediction": prediction,
            "confidence": confidence,
            "malignant_percentage": malignant_percentage,
            "image_url": image_url,
            "gradcam_url": gradcam_url,
            "patient_name": patient_name,
            "patient_id": patient_id,
            "pdf_url": pdf_url
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Operation failed: {str(e)}")


@app.get("/scans")
async def get_scans(current_doctor: dict = Depends(get_current_doctor)):
    try:
        res = supabase.table("scans").select("*").eq("doctor_id", current_doctor["id"]).order("scan_date", desc=True).execute()
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download-report/{scan_id}")
async def download_report(scan_id: str):
    # In Supabase version, we just redirect to the public URL or fetch from storage
    pdf_filename = f"Report_{scan_id}.pdf"
    try:
        url = supabase.storage.from_("reports").get_public_url(pdf_filename)
        return {"url": url}
    except Exception:
        raise HTTPException(status_code=404, detail="Report not found")


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
