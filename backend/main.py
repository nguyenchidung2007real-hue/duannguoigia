from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, date
import random
import re
import secrets

from database import engine, Base, get_db
import models
import schemas
from services.medical_guardrails import evaluate_blood_pressure

# 1. Khởi tạo bảng CSDL
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="AnTam AI Backend API",
    description="Hệ thống Trợ lý Chăm sóc Sức khỏe & Đồng hành cùng Người Cao Tuổi",
    version="1.0.0"
)

# 2. CORS Middleware (Cho phép Frontend gọi vào)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Quản lý WebSocket kết nối Real-time giữa Cụ và Con cái
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Xử lý ping/pong nếu cần
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ========================================================
# 4. API PHÂN HỆ NGƯỜI CAO TUỔI (ZERO-FRICTION)
# ========================================================

@app.post("/api/elderly/create", response_model=schemas.ElderlyProfileResponse)
def create_elderly_profile(profile: schemas.ElderlyProfileCreate, db: Session = Depends(get_db)):
    """Tạo mới hồ sơ người già (Con cái tạo, hệ thống sinh mã 6 số)"""
    pairing_code = f"{random.randint(100000, 999999)}"
    emergency_qr = secrets.token_hex(16)
    
    db_profile = models.ElderlyProfile(
        full_name=profile.full_name,
        birth_year=profile.birth_year,
        gender=profile.gender,
        address=profile.address,
        medical_history=profile.medical_history,
        drug_allergies=profile.drug_allergies,
        pairing_code=pairing_code,
        emergency_qr_token=emergency_qr
    )
    db.add(db_profile)
    db.commit()
    db.refresh(db_profile)
    return db_profile

@app.post("/api/elderly/pair", response_model=schemas.ElderlyProfileResponse)
def pair_device(req: schemas.PairRequest, db: Session = Depends(get_db)):
    """Cụ nhập mã 6 số (hoặc con nhập hộ) để đăng nhập 1 lần vĩnh viễn không mật khẩu"""
    profile = db.query(models.ElderlyProfile).filter(models.ElderlyProfile.pairing_code == req.pairing_code).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Mã ghép nối không đúng. Cụ kiểm tra lại giúp con nhé!")
    
    profile.last_seen_at = datetime.utcnow()
    db.commit()
    return profile

@app.get("/api/elderly/{elderly_id}/medications/today")
def get_today_medications(elderly_id: str, db: Session = Depends(get_db)):
    """Lấy danh sách thuốc cữ hôm nay cho màn hình người già (Thẻ thuốc to)"""
    meds = db.query(models.MedicationSchedule).filter(
        models.MedicationSchedule.elderly_id == elderly_id,
        models.MedicationSchedule.is_active == True
    ).all()

    today_str = date.today().isoformat()
    result = []
    
    for med in meds:
        # Lấy lịch sử hôm nay xem đã uống chưa
        log = db.query(models.MedicationLog).filter(
            models.MedicationLog.medication_id == med.id,
            models.MedicationLog.scheduled_time >= datetime.combine(date.today(), datetime.min.time())
        ).first()

        status = log.status if log else "PENDING"
        result.append({
            "medication_id": med.id,
            "medicine_name": med.medicine_name,
            "pill_color_shape": med.pill_color_shape,
            "dosage": med.dosage,
            "instructions": med.instructions,
            "schedule_times": med.schedule_times,
            "status": status,
            "is_taken": (status == "TAKEN")
        })
    return result

@app.post("/api/elderly/{elderly_id}/medications/{med_id}/take")
async def take_medication(elderly_id: str, med_id: str, req: schemas.MedicationTakeRequest, db: Session = Depends(get_db)):
    """Cụ bấm nút to 'ĐÃ UỐNG THUỐC' hoặc nói bằng giọng nói"""
    med = db.query(models.MedicationSchedule).filter(models.MedicationSchedule.id == med_id).first()
    if not med:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn thuốc")

    # Tạo hoặc cập nhật log
    now = datetime.utcnow()
    log = models.MedicationLog(
        medication_id=med.id,
        elderly_id=elderly_id,
        scheduled_time=now,
        actual_taken_time=now,
        status="TAKEN",
        confirmation_method=req.confirmation_method,
        reminder_count=1
    )
    db.add(log)
    db.commit()

    # Bắn thông báo real-time sang màn hình của con cái
    await manager.broadcast({
        "event": "MEDICATION_TAKEN",
        "elderly_id": elderly_id,
        "medicine_name": med.medicine_name,
        "time": now.strftime("%H:%M - %d/%m/%Y")
    })

    return {
        "success": True,
        "message": f"Dạ con đã ghi nhận cụ uống thuốc {med.medicine_name} rồi ạ. Cụ uống thêm ngụm nước ấm nhé!"
    }


# ========================================================
# 5. API ĐỌC HUYẾT ÁP BẰNG GIỌNG NÓI & GUARDRAIL
# ========================================================

def extract_bp_from_text(text: str):
    """Bóc tách số đo huyết áp từ câu nói tiếng Việt"""
    # Tìm các mẫu số dạng: 135/85 hoặc 135 trên 85 hoặc 135 và 85
    match = re.search(r'(\d{2,3})\s*(?:trên|\/|và|-)\s*(\d{2,3})', text)
    pulse_match = re.search(r'(?:nhịp tim|tim)\s*(\d{2,3})', text)
    
    systolic = float(match.group(1)) if match else None
    diastolic = float(match.group(2)) if match else None
    pulse = float(pulse_match.group(1)) if pulse_match else None
    return systolic, diastolic, pulse

@app.post("/api/elderly/vitals/voice-log")
async def log_vitals_by_voice(data: schemas.VitalSignVoiceInput, db: Session = Depends(get_db)):
    """Cụ đọc: 'Huyết áp 135 trên 85, nhịp tim 75' -> Bóc tách và kiểm tra Guardrail"""
    sys_val, dia_val, pulse_val = data.systolic, data.diastolic, data.heart_rate

    if not sys_val or not dia_val:
        # Tự bóc tách từ text
        sys_val, dia_val, pulse_val = extract_bp_from_text(data.raw_transcript)

    if not sys_val or not dia_val:
        return {
            "success": False,
            "need_retry": True,
            "message": "Dạ con chưa nghe rõ hai chỉ số huyết áp. Cụ đọc lại ví dụ '130 trên 80' giúp con nhé ạ!"
        }

    # Đưa qua Rào chắn Y tế cứng (Medical Guardrail)
    eval_result = evaluate_blood_pressure(sys_val, dia_val, pulse_val)

    # Lưu vào Database
    db_vital = models.VitalSign(
        elderly_id=data.elderly_id,
        systolic=sys_val,
        diastolic=dia_val,
        heart_rate=pulse_val,
        health_category=eval_result["category"],
        input_source="VOICE_INPUT",
        raw_voice_transcript=data.raw_transcript
    )
    db.add(db_vital)
    db.commit()

    # Bắn thông báo sang Dashboard con cái
    await manager.broadcast({
        "event": "VITAL_LOGGED",
        "elderly_id": data.elderly_id,
        "systolic": sys_val,
        "diastolic": dia_val,
        "heart_rate": pulse_val,
        "category": eval_result["category"]
    })

    return {
        "success": True,
        "systolic": sys_val,
        "diastolic": dia_val,
        "heart_rate": pulse_val,
        "category": eval_result["category"],
        "advice": eval_result["advice"]
    }

@app.get("/api/elderly/{elderly_id}/vitals/history")
def get_vitals_history(elderly_id: str, db: Session = Depends(get_db)):
    """Lấy dữ liệu để con cái vẽ biểu đồ huyết áp"""
    records = db.query(models.VitalSign).filter(
        models.VitalSign.elderly_id == elderly_id
    ).order_by(models.VitalSign.measured_at.asc()).all()

    return [
        {
            "id": r.id,
            "systolic": r.systolic,
            "diastolic": r.diastolic,
            "heart_rate": r.heart_rate,
            "category": r.health_category,
            "measured_at": r.measured_at.strftime("%d/%m %H:%M")
        } for r in records
    ]


# ========================================================
# 6. KHỞI CHẠY SEED DATA MẪU (ĐỂ TEST ĐƯỢC NGAY)
# ========================================================
@app.on_event("startup")
def startup_seed_data():
    db = SessionLocal()
    # Kiểm tra nếu chưa có cụ nào thì tạo mẫu cụ Lan
    existing = db.query(models.ElderlyProfile).first()
    if not existing:
        cụ_lan = models.ElderlyProfile(
            full_name="Bà Nguyễn Thị Lan",
            birth_year=1952,
            gender="FEMALE",
            address="Số 45, Phố Huế, Hai Bà Trưng, Hà Nội",
            medical_history="Tăng huyết áp vô căn 5 năm, thoái hóa khớp gối nhẹ",
            drug_allergies="Dị ứng kháng sinh Penicillin",
            pairing_code="123456", # Mã dễ test
            emergency_qr_token="qr_token_lan_1952"
        )
        db.add(cụ_lan)
        db.commit()
        db.refresh(cụ_lan)

        # Thêm 2 đơn thuốc huyết áp mẫu
        thuoc_1 = models.MedicationSchedule(
            elderly_id=cụ_lan.id,
            medicine_name="Amlodipine 5mg",
            pill_color_shape="Viên tròn nhỏ màu vàng",
            dosage="1 viên",
            instructions="Uống sau ăn sáng",
            schedule_times=["08:00"]
        )
        thuoc_2 = models.MedicationSchedule(
            elderly_id=cụ_lan.id,
            medicine_name="Glucophage 500mg",
            pill_color_shape="Viên con nhộng trắng",
            dosage="1 viên",
            instructions="Uống sau ăn tối",
            schedule_times=["19:30"]
        )
        db.add_all([thuoc_1, thuoc_2])
        db.commit()
    db.close()
