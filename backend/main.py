from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date, timedelta
import random
import re
import secrets

from database import engine, Base, get_db, SessionLocal
import models
import schemas

try:
    from services.medical_guardrails import evaluate_blood_pressure, preprocess_voice_text, normalize_bp_value
except ModuleNotFoundError:
    try:
        from backend.services.medical_guardrails import evaluate_blood_pressure, preprocess_voice_text, normalize_bp_value
    except ModuleNotFoundError:
        from medical_guardrails import evaluate_blood_pressure, preprocess_voice_text, normalize_bp_value

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="AnTam AI Backend API",
    description="Hệ thống Trợ lý Chăm sóc Sức khỏe & Đồng hành cùng Người Cao Tuổi",
    version="2.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
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
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ========================================================
# 1. API NGƯỜI CAO TUỔI (ZERO-FRICTION & LỊCH THUỐC)
# ========================================================

@app.post("/api/elderly/create", response_model=schemas.ElderlyProfileResponse)
def create_elderly_profile(profile: schemas.ElderlyProfileCreate, db: Session = Depends(get_db)):
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
    profile = db.query(models.ElderlyProfile).filter(models.ElderlyProfile.pairing_code == req.pairing_code).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Mã ghép nối không đúng. Cụ kiểm tra lại giúp con nhé!")
    
    profile.last_seen_at = datetime.now()
    db.commit()
    return profile

@app.get("/api/elderly/{elderly_id}/medications/today")
def get_today_medications(elderly_id: str, db: Session = Depends(get_db)):
    meds = db.query(models.MedicationSchedule).filter(
        models.MedicationSchedule.elderly_id == elderly_id,
        models.MedicationSchedule.is_active == True
    ).all()

    today_start = datetime.combine(date.today(), datetime.min.time())
    result = []
    
    for med in meds:
        log = db.query(models.MedicationLog).filter(
            models.MedicationLog.medication_id == med.id,
            models.MedicationLog.scheduled_time >= today_start
        ).order_by(models.MedicationLog.scheduled_time.desc()).first()

        status = log.status if log else "PENDING"
        taken_time_str = log.actual_taken_time.strftime("%H:%M") if log and log.actual_taken_time else None

        result.append({
            "medication_id": med.id,
            "medicine_name": med.medicine_name,
            "pill_color_shape": med.pill_color_shape,
            "dosage": med.dosage,
            "instructions": med.instructions,
            "schedule_times": med.schedule_times,
            "status": status,
            "is_taken": (status == "TAKEN"),
            "taken_time": taken_time_str,
            "log_id": log.id if log else None
        })
    return result

@app.post("/api/elderly/{elderly_id}/medications/{med_id}/take")
async def take_medication(elderly_id: str, med_id: str, req: schemas.MedicationTakeRequest, db: Session = Depends(get_db)):
    med = db.query(models.MedicationSchedule).filter(models.MedicationSchedule.id == med_id).first()
    if not med:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn thuốc")

    now = datetime.now()
    ten_mins_ago = now - timedelta(minutes=10)

    recent_log = db.query(models.MedicationLog).filter(
        models.MedicationLog.medication_id == med.id,
        models.MedicationLog.actual_taken_time >= ten_mins_ago,
        models.MedicationLog.status == "TAKEN"
    ).first()

    if recent_log:
        return {
            "success": True,
            "already_taken": True,
            "log_id": recent_log.id,
            "message": f"Dạ con đã ghi nhận cụ uống thuốc {med.medicine_name} lúc {recent_log.actual_taken_time.strftime('%H:%M')} rồi ạ!"
        }

    new_log = models.MedicationLog(
        medication_id=med.id,
        elderly_id=elderly_id,
        scheduled_time=now,
        actual_taken_time=now,
        status="TAKEN",
        confirmation_method=req.confirmation_method,
        reminder_count=1
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)

    await manager.broadcast({
        "event": "MEDICATION_TAKEN",
        "elderly_id": elderly_id,
        "medicine_name": med.medicine_name,
        "time": now.strftime("%H:%M - %d/%m/%Y")
    })

    return {
        "success": True,
        "already_taken": False,
        "log_id": new_log.id,
        "message": f"Dạ con đã ghi nhận cụ uống thuốc {med.medicine_name} rồi ạ. Cụ uống thêm ngụm nước ấm nhé!"
    }

@app.post("/api/elderly/medications/log/{log_id}/undo")
async def undo_take_medication(log_id: str, db: Session = Depends(get_db)):
    log = db.query(models.MedicationLog).filter(models.MedicationLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")
    
    db.delete(log)
    db.commit()

    await manager.broadcast({
        "event": "MEDICATION_UNDO",
        "log_id": log_id,
        "time": datetime.now().strftime("%H:%M")
    })

    return {"success": True, "message": "Đã hoàn tác thành công."}


# =========================================================================
# 2. BÓC TÁCH HUYẾT ÁP GIỌNG NÓI ĐA DẠNG & QUY TRÌNH 2 BƯỚC
# =========================================================================

def extract_bp_from_text(raw_text: str):
    """
    Bóc tách số đo huyết áp sau khi đã tiền xử lý 'rưỡi', dấu phẩy, v.v.
    Hỗ trợ đa dạng các kịch bản thực tế của người cao tuổi:
    - Có từ nối: '135 trên 85', '135/85', '135 và 85', '135 tâm trương 85'
    - Đọc theo màn hình máy đo 3 số: '120 80 75', '13 8 72' (Tâm thu, Tâm trương, Nhịp tim)
    - Đọc liền 2 số: 'huyết áp 135 85', '13 8', '12 5 8 5' (12.5 8.5)
    - Bắt nhịp tim riêng: 'nhịp tim 75', 'tim 78', 'mạch 80' (tách riêng để không lẫn với huyết áp)
    - Nhận diện khi chỉ có 1 số: 'huyết áp 12 5 nhịp tim 70' -> bắt được 12.5 (125), tâm trương thiếu
    """
    clean_text = preprocess_voice_text(raw_text)

    # 1. Bắt nhịp tim nếu có từ khóa tường minh và tách khỏi văn bản huyết áp
    pulse = None
    pulse_match = re.search(r'(?:nhịp tim|tim|mạch)\s*(?:là|khoảng)?\s*(\d{2,3})', clean_text)
    if pulse_match:
        pulse = float(pulse_match.group(1))
        # Loại bỏ đoạn nhịp tim để không làm nhiễu bóc tách huyết áp
        bp_clean = clean_text[:pulse_match.start()] + ' ' + clean_text[pulse_match.end():]
    else:
        bp_clean = clean_text

    sys_raw, dia_raw = None, None

    # Ưu tiên 1: Có từ nối rõ ràng ('trên', '/', 'và', '-', 'tâm trương')
    match_sep = re.search(r'(\d+(?:\.\d+)?)\s*(?:trên|\/|và|-|tâm trương)\s*(\d+(?:\.\d+)?)', bp_clean)
    if match_sep:
        sys_raw = float(match_sep.group(1))
        dia_raw = float(match_sep.group(2))
    else:
        # Ưu tiên 2: Cụ đọc 3 số liên tiếp từ màn hình máy đo (VD: "120 80 75" hoặc "13 8 72")
        match_3 = re.search(r'(\d{1,3}(?:\.\d+)?)\s+(\d{1,3}(?:\.\d+)?)\s+(\d{2,3})', bp_clean)
        if match_3 and pulse is None:
            sys_raw = float(match_3.group(1))
            dia_raw = float(match_3.group(2))
            pulse = float(match_3.group(3))
        else:
            # Ưu tiên 3: 2 số liên tiếp (VD: "135 85", "13 8", "12.5 8.5", "12.5 80")
            match_2 = re.search(r'(\d{1,3}(?:\.\d+)?)\s+(\d{1,3}(?:\.\d+)?)', bp_clean)
            if match_2:
                sys_raw = float(match_2.group(1))
                dia_raw = float(match_2.group(2))
            else:
                # Ưu tiên 4: Cụ chỉ đọc 1 số duy nhất (VD: "huyết áp 12 5" -> 12.5, "130", "tâm trương 80")
                single_matches = re.findall(r'\b\d+(?:\.\d+)?\b', bp_clean)
                if len(single_matches) == 1:
                    val = float(single_matches[0])
                    if 'tâm trương' in bp_clean or 'số dưới' in bp_clean:
                        dia_raw = val
                    else:
                        sys_raw = val

    systolic = normalize_bp_value(sys_raw)
    diastolic = normalize_bp_value(dia_raw)

    return systolic, diastolic, pulse

@app.post("/api/elderly/vitals/parse-voice")
def parse_voice_vitals(data: schemas.VitalSignParseInput):
    sys_val, dia_val, pulse_val = extract_bp_from_text(data.raw_transcript)

    # Xử lý các tình huống biên khi chỉ bắt được 1 số hoặc không bắt được số nào (dùng is None để tránh lỗi số 0 bị falsy)
    if sys_val is not None and dia_val is None:
        return {
            "success": False,
            "need_retry": True,
            "message": f"Dạ con mới chỉ nghe được một chỉ số huyết áp là {sys_val}. Cụ đọc giúp con đủ cả hai số, số trên và số dưới (ví dụ '{sys_val} trên 80') nhé ạ!"
        }

    if sys_val is None and dia_val is not None:
        return {
            "success": False,
            "need_retry": True,
            "message": f"Dạ con mới chỉ nghe được chỉ số tâm trương là {dia_val}. Cụ đọc giúp con đủ cả hai số, số trên và số dưới (ví dụ '120 trên {dia_val}') nhé ạ!"
        }

    if sys_val is None or dia_val is None:
        return {
            "success": False,
            "need_retry": True,
            "message": "Dạ con chưa nghe rõ hai số huyết áp. Cụ đọc lại ví dụ '130 trên 80' hoặc '13 trên 8' giúp con nhé ạ!"
        }

    eval_result = evaluate_blood_pressure(sys_val, dia_val, pulse_val)

    if eval_result.get("category") == "INVALID":
        return {
            "success": False,
            "need_retry": True,
            "message": eval_result["advice"]
        }

    pulse_text = f", nhịp tim {int(pulse_val)} nhịp mỗi phút" if pulse_val else ""
    confirm_prompt = (
        f"Dạ con nghe số trên là {eval_result['systolic']}, số dưới là {eval_result['diastolic']}{pulse_text}. "
        f"Đúng rồi thì cụ bấm nút Xanh 'ĐÚNG RỒI', nếu chưa đúng cụ bấm nút ĐỎ đọc lại giúp con nhé ạ!"
    )

    return {
        "success": True,
        "need_confirmation": True,
        "systolic": eval_result["systolic"],
        "diastolic": eval_result["diastolic"],
        "heart_rate": pulse_val,
        "pulse_status": eval_result.get("pulse_status", "NORMAL"),
        "category": eval_result["category"],
        "title": eval_result["title"],
        "is_emergency": eval_result.get("is_emergency", False),
        "confirm_prompt": confirm_prompt,
        "advice": eval_result["advice"]
    }

@app.post("/api/elderly/vitals/confirm-log")
async def confirm_log_vitals(data: schemas.VitalSignConfirmInput, db: Session = Depends(get_db)):
    eval_result = evaluate_blood_pressure(data.systolic, data.diastolic, data.heart_rate)

    now = datetime.now()
    db_vital = models.VitalSign(
        elderly_id=data.elderly_id,
        systolic=eval_result["systolic"],
        diastolic=eval_result["diastolic"],
        heart_rate=data.heart_rate,
        health_category=eval_result["category"],
        input_source="VOICE_INPUT",
        raw_voice_transcript=data.raw_transcript,
        measured_at=now
    )
    db.add(db_vital)
    db.commit()
    db.refresh(db_vital)

    now_str = now.strftime("%H:%M - %d/%m")

    await manager.broadcast({
        "event": "VITAL_LOGGED",
        "elderly_id": data.elderly_id,
        "systolic": eval_result["systolic"],
        "diastolic": eval_result["diastolic"],
        "heart_rate": data.heart_rate,
        "category": eval_result["category"],
        "title": eval_result["title"],
        "is_emergency": eval_result.get("is_emergency", False),
        "time": now_str
    })

    return {
        "success": True,
        "saved_id": db_vital.id,
        "advice": eval_result["advice"],
        "message": f"Dạ con đã lưu chỉ số {eval_result['systolic']}/{eval_result['diastolic']} vào sổ sức khỏe rồi ạ. {eval_result['advice']}"
    }

@app.get("/api/elderly/{elderly_id}/vitals/history")
def get_vitals_history(elderly_id: str, db: Session = Depends(get_db)):
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
# 3. API CON CÁI (FAMILY / CAREGIVER DASHBOARD)
# ========================================================

@app.get("/api/family/summary/{elderly_id}")
def get_family_summary(elderly_id: str, db: Session = Depends(get_db)):
    profile = db.query(models.ElderlyProfile).filter(models.ElderlyProfile.id == elderly_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Không tìm thấy hồ sơ người cao tuổi")

    today_start = datetime.combine(date.today(), datetime.min.time())
    total_meds = db.query(models.MedicationSchedule).filter(
        models.MedicationSchedule.elderly_id == elderly_id,
        models.MedicationSchedule.is_active == True
    ).count()

    taken_count = db.query(models.MedicationLog).filter(
        models.MedicationLog.elderly_id == elderly_id,
        models.MedicationLog.scheduled_time >= today_start,
        models.MedicationLog.status == "TAKEN"
    ).count()

    latest_vital = db.query(models.VitalSign).filter(
        models.VitalSign.elderly_id == elderly_id
    ).order_by(models.VitalSign.measured_at.desc()).first()

    compliance = round((taken_count / total_meds * 100), 1) if total_meds > 0 else 100.0

    return {
        "profile": {
            "id": profile.id,
            "full_name": profile.full_name,
            "birth_year": profile.birth_year,
            "medical_history": profile.medical_history,
            "pairing_code": profile.pairing_code
        },
        "medication_stats": {
            "total_today": total_meds,
            "taken_today": taken_count,
            "compliance_rate": compliance
        },
        "latest_vital": {
            "systolic": latest_vital.systolic if latest_vital else None,
            "diastolic": latest_vital.diastolic if latest_vital else None,
            "heart_rate": latest_vital.heart_rate if latest_vital else None,
            "category": latest_vital.health_category if latest_vital else None,
            "time": latest_vital.measured_at.strftime("%H:%M %d/%m") if latest_vital else None
        }
    }


# ========================================================
# 4. SEED DATA KHỞI ĐỘNG
# ========================================================
@app.on_event("startup")
def startup_seed_data():
    db = SessionLocal()
    existing = db.query(models.ElderlyProfile).first()
    if not existing:
        cụ_lan = models.ElderlyProfile(
            id="lan_1952",
            full_name="Bà Nguyễn Thị Lan",
            birth_year=1952,
            gender="FEMALE",
            address="Số 45, Phố Huế, Hai Bà Trưng, Hà Nội",
            medical_history="Tăng huyết áp vô căn 5 năm, thoái hóa khớp gối nhẹ",
            drug_allergies="Dị ứng kháng sinh Penicillin",
            pairing_code="123456",
            emergency_qr_token="qr_lan_1952"
        )
        db.add(cụ_lan)
        db.commit()
        db.refresh(cụ_lan)

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

        now = datetime.now()
        v1 = models.VitalSign(elderly_id=cụ_lan.id, systolic=125, diastolic=80, heart_rate=76, health_category="NORMAL", measured_at=now - timedelta(days=2))
        v2 = models.VitalSign(elderly_id=cụ_lan.id, systolic=132, diastolic=84, heart_rate=78, health_category="HIGH_NORMAL", measured_at=now - timedelta(days=1))
        v3 = models.VitalSign(elderly_id=cụ_lan.id, systolic=135, diastolic=85, heart_rate=75, health_category="HIGH_NORMAL", measured_at=now - timedelta(hours=3))
        db.add_all([v1, v2, v3])

        db.commit()
    db.close()
