import asyncio
from datetime import datetime, timedelta
import html
import random
import re
import secrets
from typing import Dict, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine, get_db
import models
import schemas

try:
    from services.medical_guardrails import (
        evaluate_blood_pressure,
        normalize_bp_value,
        preprocess_voice_text,
    )
    from services.notification_service import dispatch_family_alert, DISPATCHED_NOTIFICATIONS
except ModuleNotFoundError:
    try:
        from backend.services.medical_guardrails import (
            evaluate_blood_pressure,
            normalize_bp_value,
            preprocess_voice_text,
        )
        from backend.services.notification_service import dispatch_family_alert, DISPATCHED_NOTIFICATIONS
    except ModuleNotFoundError:
        from medical_guardrails import (
            evaluate_blood_pressure,
            normalize_bp_value,
            preprocess_voice_text,
        )
        from notification_service import dispatch_family_alert, DISPATCHED_NOTIFICATIONS

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="AnTam AI Backend API",
    description="Hệ thống Trợ lý Chăm sóc Sức khỏe & Đồng hành cùng Người Cao Tuổi",
    version="2.3.0",
)

app.add_middleware(
    CORSMiddleware,
    # ponytail: whitelist origins, add env-var config khi deploy production
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "null"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory dictionary for pairing code brute-force protection: pairing_code -> list of failure timestamps
FAILED_PAIR_ATTEMPTS: Dict[str, List[datetime]] = {}


# ========================================================
# 0. QUẢN LÝ KẾT NỐI WEBSOCKET PHÂN PHÒNG THEO NGƯỜI CAO TUỔI
# ========================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.connection_rooms: Dict[WebSocket, Set[str]] = {}

    async def connect(
        self, websocket: WebSocket, elderly_id: Optional[str] = None
    ):
        await websocket.accept()
        self.active_connections.append(websocket)
        if elderly_id:
            if elderly_id not in self.rooms:
                self.rooms[elderly_id] = set()
            self.rooms[elderly_id].add(websocket)
            if websocket not in self.connection_rooms:
                self.connection_rooms[websocket] = set()
            self.connection_rooms[websocket].add(elderly_id)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.connection_rooms:
            for elderly_id in self.connection_rooms[websocket]:
                if (
                    elderly_id in self.rooms
                    and websocket in self.rooms[elderly_id]
                ):
                    self.rooms[elderly_id].remove(websocket)
                    if not self.rooms[elderly_id]:
                        del self.rooms[elderly_id]
            del self.connection_rooms[websocket]

    async def broadcast(
        self, message: dict, elderly_id: Optional[str] = None
    ):
        target_elderly = elderly_id or message.get("elderly_id")
        targets: Set[WebSocket] = set()

        # Thêm các kết nối đăng ký theo đúng người cao tuổi
        if target_elderly and target_elderly in self.rooms:
            targets.update(self.rooms[target_elderly])

        # Thêm các kết nối toàn cục (người nghe chung không chỉ định elderly_id)
        for conn in self.active_connections:
            if conn not in self.connection_rooms or not self.connection_rooms[conn]:
                targets.add(conn)

        # L2 fix: Nếu không có targets cụ thể, không fallback broadcast-all (privacy leak)
        # chỉ gửi tới global listeners không thuộc room nào
        if not targets:
            return

        for connection in list(targets):
            try:
                await connection.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket, elderly_id: Optional[str] = None
):
    await manager.connect(websocket, elderly_id=elderly_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ========================================================
# 1. API NGƯỜI CAO TUỔI (ZERO-FRICTION & LỊCH THUỐC)
# ========================================================


@app.post("/api/elderly/create", response_model=schemas.ElderlyProfileResponse)
def create_elderly_profile(
    profile: schemas.ElderlyProfileCreate, db: Session = Depends(get_db)
):
    # Sinh mã ghép nối ngẫu nhiên 6 chữ số đảm bảo tính duy nhất
    while True:
        pairing_code = f"{secrets.randbelow(900000) + 100000}"
        existing = (
            db.query(models.ElderlyProfile)
            .filter(models.ElderlyProfile.pairing_code == pairing_code)
            .first()
        )
        if not existing:
            break

    emergency_qr = secrets.token_hex(16)

    db_profile = models.ElderlyProfile(
        full_name=profile.full_name,
        birth_year=profile.birth_year,
        gender=profile.gender,
        address=profile.address,
        medical_history=profile.medical_history,
        drug_allergies=profile.drug_allergies,
        pairing_code=pairing_code,
        emergency_qr_token=emergency_qr,
    )
    db.add(db_profile)
    db.commit()
    db.refresh(db_profile)
    return db_profile


@app.post("/api/elderly/pair", response_model=schemas.ElderlyProfileResponse)
def pair_device(req: schemas.PairRequest, db: Session = Depends(get_db)):
    now = models.get_vn_now()
    code = req.pairing_code.strip()

    # Chống dò mã Brute-force: Tối đa 5 lần sai trong vòng 10 phút
    recent_fails = [
        t
        for t in FAILED_PAIR_ATTEMPTS.get(code, [])
        if (now - t).total_seconds() < 600
    ]
    FAILED_PAIR_ATTEMPTS[code] = recent_fails

    if len(recent_fails) >= 5:
        raise HTTPException(
            status_code=429,
            detail="Đã nhập sai mã ghép nối quá 5 lần. Vui lòng đợi 10 phút trước khi thử lại.",
        )

    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.pairing_code == code)
        .first()
    )
    if not profile:
        FAILED_PAIR_ATTEMPTS[code].append(now)
        raise HTTPException(
            status_code=404,
            detail="Mã ghép nối không đúng. Cụ kiểm tra lại giúp con nhé!",
        )

    # Đăng nhập thành công -> Xóa bộ đếm thất bại
    FAILED_PAIR_ATTEMPTS.pop(code, None)
    profile.last_seen_at = now
    db.commit()
    return profile


@app.get("/api/elderly/{elderly_id}/medications/today")
def get_today_medications(elderly_id: str, db: Session = Depends(get_db)):
    # AN TOÀN Y KHOA: Chỉ tải các đơn thuốc đã được con cái/bác sĩ xác nhận (verified_by_caregiver == True)
    meds = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.elderly_id == elderly_id,
            models.MedicationSchedule.is_active == True,
            models.MedicationSchedule.verified_by_caregiver == True,
        )
        .all()
    )

    now = models.get_vn_now()
    today_start = datetime.combine(now.date(), datetime.min.time())
    result = []

    for med in meds:
        log = (
            db.query(models.MedicationLog)
            .filter(
                models.MedicationLog.medication_id == med.id,
                models.MedicationLog.scheduled_time >= today_start,
            )
            .order_by(models.MedicationLog.scheduled_time.desc())
            .first()
        )

        status = log.status if log else "PENDING"
        taken_time_str = (
            log.actual_taken_time.strftime("%H:%M")
            if log and log.actual_taken_time
            else None
        )

        result.append(
            {
                "medication_id": med.id,
                "medicine_name": med.medicine_name,
                "pill_color_shape": med.pill_color_shape,
                "dosage": med.dosage,
                "instructions": med.instructions,
                "schedule_times": med.schedule_times,
                "status": status,
                "is_taken": (status == "TAKEN"),
                "taken_time": taken_time_str,
                "log_id": log.id if log else None,
            }
        )
    return result


@app.post("/api/elderly/{elderly_id}/medications/{med_id}/take")
async def take_medication(
    elderly_id: str,
    med_id: str,
    req: schemas.MedicationTakeRequest,
    db: Session = Depends(get_db),
):
    med = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.id == med_id,
            models.MedicationSchedule.elderly_id == elderly_id,  # IDOR fix C3
        )
        .first()
    )
    if not med:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn thuốc")

    now = models.get_vn_now()
    ten_mins_ago = now - timedelta(minutes=10)

    recent_log = (
        db.query(models.MedicationLog)
        .filter(
            models.MedicationLog.medication_id == med.id,
            models.MedicationLog.actual_taken_time >= ten_mins_ago,
            models.MedicationLog.status == "TAKEN",
        )
        .first()
    )

    if recent_log:
        return {
            "success": True,
            "already_taken": True,
            "log_id": recent_log.id,
            "message": f"Dạ con đã ghi nhận cụ uống thuốc {med.medicine_name} lúc {recent_log.actual_taken_time.strftime('%H:%M')} rồi ạ!",
        }

    new_log = models.MedicationLog(
        medication_id=med.id,
        elderly_id=elderly_id,
        scheduled_time=now,
        actual_taken_time=now,
        status="TAKEN",
        confirmation_method=req.confirmation_method,
        reminder_count=1,
    )
    db.add(new_log)
    db.commit()
    db.refresh(new_log)

    await manager.broadcast(
        {
            "event": "MEDICATION_TAKEN",
            "elderly_id": elderly_id,
            "medicine_name": med.medicine_name,
            "time": now.strftime("%H:%M - %d/%m/%Y"),
        },
        elderly_id=elderly_id,
    )

    return {
        "success": True,
        "already_taken": False,
        "log_id": new_log.id,
        "message": f"Dạ con đã ghi nhận cụ uống thuốc {med.medicine_name} rồi ạ. Cụ uống thêm ngụm nước ấm nhé!",
    }


@app.post("/api/elderly/{elderly_id}/medications/log/{log_id}/undo")
async def undo_take_medication(elderly_id: str, log_id: str, db: Session = Depends(get_db)):
    log = (
        db.query(models.MedicationLog)
        .filter(
            models.MedicationLog.id == log_id,
            models.MedicationLog.elderly_id == elderly_id,  # IDOR fix C2
        )
        .first()
    )
    if not log:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi")

    elderly_id = log.elderly_id
    now = models.get_vn_now()
    db.delete(log)
    db.commit()

    await manager.broadcast(
        {
            "event": "MEDICATION_UNDO",
            "elderly_id": elderly_id,
            "log_id": log_id,
            "time": now.strftime("%H:%M"),
        },
        elderly_id=elderly_id,
    )

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
    pulse_match = re.search(
        r"(?:nhịp tim|tim|mạch)\s*(?:là|khoảng)?\s*(\d{2,3})", clean_text
    )
    if pulse_match:
        pulse = float(pulse_match.group(1))
        # Loại bỏ đoạn nhịp tim để không làm nhiễu bóc tách huyết áp
        bp_clean = (
            clean_text[: pulse_match.start()]
            + " "
            + clean_text[pulse_match.end() :]
        )
    else:
        bp_clean = clean_text

    sys_raw, dia_raw = None, None

    # Ưu tiên 1: Có từ nối rõ ràng ('trên', '/', 'và', '-', 'tâm trương')
    match_sep = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:trên|\/|và|-|tâm trương)\s*(\d+(?:\.\d+)?)",
        bp_clean,
    )
    if match_sep:
        sys_raw = float(match_sep.group(1))
        dia_raw = float(match_sep.group(2))
    else:
        # Ưu tiên 2: Cụ đọc 3 số liên tiếp từ màn hình máy đo (VD: "120 80 75" hoặc "13 8 72")
        match_3 = re.search(
            r"(\d{1,3}(?:\.\d+)?)\s+(\d{1,3}(?:\.\d+)?)\s+(\d{2,3})", bp_clean
        )
        if match_3 and pulse is None:
            sys_raw = float(match_3.group(1))
            dia_raw = float(match_3.group(2))
            pulse = float(match_3.group(3))
        else:
            # Ưu tiên 3: 2 số liên tiếp (VD: "135 85", "13 8", "12.5 8.5", "12.5 80")
            match_2 = re.search(
                r"(\d{1,3}(?:\.\d+)?)\s+(\d{1,3}(?:\.\d+)?)", bp_clean
            )
            if match_2:
                sys_raw = float(match_2.group(1))
                dia_raw = float(match_2.group(2))
            else:
                # Ưu tiên 4: Cụ chỉ đọc 1 số duy nhất (VD: "huyết áp 12 5" -> 12.5, "130", "tâm trương 80")
                single_matches = re.findall(r"\b\d+(?:\.\d+)?\b", bp_clean)
                if len(single_matches) == 1:
                    val = float(single_matches[0])
                    if "tâm trương" in bp_clean or "số dưới" in bp_clean:
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
            "message": f"Dạ con mới chỉ nghe được một chỉ số huyết áp là {sys_val}. Cụ đọc giúp con đủ cả hai số, số trên và số dưới (ví dụ '{sys_val} trên 80') nhé ạ!",
        }

    if sys_val is None and dia_val is not None:
        return {
            "success": False,
            "need_retry": True,
            "message": f"Dạ con mới chỉ nghe được chỉ số tâm trương là {dia_val}. Cụ đọc giúp con đủ cả hai số, số trên và số dưới (ví dụ '120 trên {dia_val}') nhé ạ!",
        }

    if sys_val is None or dia_val is None:
        return {
            "success": False,
            "need_retry": True,
            "message": "Dạ con chưa nghe rõ hai số huyết áp. Cụ đọc lại ví dụ '130 trên 80' hoặc '13 trên 8' giúp con nhé ạ!",
        }

    eval_result = evaluate_blood_pressure(sys_val, dia_val, pulse_val)

    if eval_result.get("category") == "INVALID":
        return {
            "success": False,
            "need_retry": True,
            "message": eval_result["advice"],
        }

    pulse_text = (
        f", nhịp tim {int(pulse_val)} nhịp mỗi phút" if pulse_val else ""
    )
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
        "advice": eval_result["advice"],
    }


@app.post("/api/elderly/vitals/confirm-log")
async def confirm_log_vitals(
    data: schemas.VitalSignConfirmInput, db: Session = Depends(get_db)
):
    eval_result = evaluate_blood_pressure(
        data.systolic, data.diastolic, data.heart_rate
    )

    # Chốt chặn Y khoa: Tuyệt đối không cho ghi vào DB nếu chỉ số INVALID
    if eval_result.get("category") == "INVALID":
        raise HTTPException(
            status_code=400,
            detail=eval_result.get(
                "advice", "Chỉ số huyết áp không hợp lệ theo quy chuẩn y tế."
            ),
        )

    now = models.get_vn_now()
    db_vital = models.VitalSign(
        elderly_id=data.elderly_id,
        systolic=eval_result["systolic"],
        diastolic=eval_result["diastolic"],
        heart_rate=data.heart_rate,
        health_category=eval_result["category"],
        input_source="VOICE_INPUT",
        raw_voice_transcript=data.raw_transcript,
        measured_at=now,
    )
    db.add(db_vital)
    db.commit()
    db.refresh(db_vital)

    now_str = now.strftime("%H:%M - %d/%m")

    # Lấy thông tin họ tên cụ thực tế để cảnh báo chính xác
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.id == data.elderly_id)
        .first()
    )
    cụ_name = profile.full_name if profile else "Người cao tuổi"

    # Nếu huyết áp ở mức nguy kịch (Severity Level 4) -> Tự động kích hoạt SOS vào HealthAndSOSLog (Có chống trùng lặp trong 10 phút)
    if eval_result.get("is_emergency"):
        ten_mins_ago = now - timedelta(minutes=10)
        recent_sos = (
            db.query(models.HealthAndSOSLog)
            .filter(
                models.HealthAndSOSLog.elderly_id == data.elderly_id,
                models.HealthAndSOSLog.trigger_source == "VITAL_CRITICAL",
                models.HealthAndSOSLog.triggered_at >= ten_mins_ago,
            )
            .first()
        )

        if not recent_sos:
            sos_entry = models.HealthAndSOSLog(
                elderly_id=data.elderly_id,
                trigger_source="VITAL_CRITICAL",
                escalation_status="BROADCASTED_FAMILY",
                location_note=f"Huyết áp nguy kịch: {eval_result['systolic']}/{eval_result['diastolic']} mmHg",
                triggered_at=now,
            )
            db.add(sos_entry)
            db.commit()

            await manager.broadcast(
                {
                    "event": "SOS_ALERT",
                    "elderly_id": data.elderly_id,
                    "full_name": cụ_name,
                    "trigger_source": "VITAL_CRITICAL",
                    "location": "Tại nhà",
                    "time": now_str,
                    "message": f"🚨 BÁO ĐỘNG ĐỎ: Huyết áp nguy kịch ({eval_result['systolic']}/{eval_result['diastolic']} mmHg) của Cụ {cụ_name} - Cần hỗ trợ khẩn cấp!",
                },
                elderly_id=data.elderly_id,
            )

    await manager.broadcast(
        {
            "event": "VITAL_LOGGED",
            "elderly_id": data.elderly_id,
            "systolic": eval_result["systolic"],
            "diastolic": eval_result["diastolic"],
            "heart_rate": data.heart_rate,
            "category": eval_result["category"],
            "title": eval_result["title"],
            "is_emergency": eval_result.get("is_emergency", False),
            "time": now_str,
        },
        elderly_id=data.elderly_id,
    )

    return {
        "success": True,
        "saved_id": db_vital.id,
        "advice": eval_result["advice"],
        "message": f"Dạ con đã lưu chỉ số {eval_result['systolic']}/{eval_result['diastolic']} vào sổ sức khỏe rồi ạ. {eval_result['advice']}",
    }


@app.get("/api/elderly/{elderly_id}/vitals/history")
def get_vitals_history(elderly_id: str, db: Session = Depends(get_db)):
    records = (
        db.query(models.VitalSign)
        .filter(models.VitalSign.elderly_id == elderly_id)
        .order_by(models.VitalSign.measured_at.asc())
        .all()
    )

    return [
        {
            "id": r.id,
            "systolic": r.systolic,
            "diastolic": r.diastolic,
            "heart_rate": r.heart_rate,
            "category": r.health_category,
            "raw_voice_transcript": r.raw_voice_transcript,
            "measured_at": r.measured_at.strftime("%d/%m %H:%M"),
        }
        for r in records
    ]


# ========================================================
# 3. API KHẨN CẤP SOS & MÃ QR CỨU HỘ
# ========================================================


@app.post("/api/elderly/{elderly_id}/sos")
async def trigger_elderly_sos(elderly_id: str, db: Session = Depends(get_db)):
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.id == elderly_id)
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=404, detail="Không tìm thấy hồ sơ người cao tuổi"
        )

    now = models.get_vn_now()
    now_str = now.strftime("%H:%M - %d/%m")

    sos_entry = models.HealthAndSOSLog(
        elderly_id=elderly_id,
        trigger_source="RED_BUTTON",
        escalation_status="BROADCASTED_FAMILY",
        location_note="Tại nhà (Cụ bấm nút SOS)",
        triggered_at=now,
    )
    db.add(sos_entry)
    db.commit()

    await manager.broadcast(
        {
            "event": "SOS_ALERT",
            "elderly_id": elderly_id,
            "full_name": profile.full_name,
            "trigger_source": "RED_BUTTON",
            "location": "Tại nhà",
            "time": now_str,
            "message": f"🚨 BÁO ĐỘNG ĐỎ: Cụ {profile.full_name} vừa bấm nút CẤP CỨU SOS tại nhà!",
        },
        elderly_id=elderly_id,
    )

    dispatch_family_alert(
        elderly_id=elderly_id,
        title=f"🚨 CẤP CỨU SOS: Cụ {profile.full_name}",
        message=f"Cụ {profile.full_name} vừa bấm nút CẤP CỨU SOS tại nhà. Xin vui lòng liên lạc hoặc kiểm tra ngay!",
        alert_type="SOS_EMERGENCY",
        db=db,
        location="Tại nhà",
    )

    return {
        "success": True,
        "message": "Dạ con đã phát chuông báo động khẩn cấp tới con cháu ngay lập tức rồi ạ! Cụ hãy ngồi nghỉ ngay tại chỗ nhé ạ!",
    }


@app.get("/qr/{token}", response_class=HTMLResponse)
def public_emergency_qr_view(token: str, db: Session = Depends(get_db)):
    """
    Trang cứu hộ khẩn cấp công cộng hiển thị khi người đi đường quét mã QR.
    - AN TOÀN: Tuyệt đối chỉ đọc (Read-only), không gây side-effect tạo SOS giả khi bot/crawler duyệt qua.
    - BẢO MẬT: Escape 100% dữ liệu để triệt tiêu lỗ hổng XSS.
    - CHÍNH XÁC: Không hiển thị số điện thoại hay địa chỉ giả mạo; nếu chưa có thì báo rõ ràng.
    """
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.emergency_qr_token == token)
        .first()
    )
    if not profile:
        return HTMLResponse(
            "<h2 style='text-align:center;margin-top:50px;color:#e11d48;'>Mã cứu hộ không tồn tại hoặc đã hết hạn.</h2>",
            status_code=404,
        )

    contact = (
        db.query(models.FamilyMember)
        .filter(
            models.FamilyMember.elderly_id == profile.id,
            models.FamilyMember.is_primary_contact == True,
        )
        .first()
    )

    safe_name = html.escape(profile.full_name or "Chưa rõ")
    safe_birth_year = profile.birth_year
    safe_address = html.escape(profile.address or "Chưa cập nhật địa chỉ nhà")
    safe_history = html.escape(profile.medical_history or "Không có")
    safe_allergies = html.escape(profile.drug_allergies or "Không rõ")

    # Xử lý thông tin liên hệ thực tế, không dùng số điện thoại giả định
    if contact and contact.phone_number:
        safe_phone = html.escape(contact.phone_number)
        safe_contact_name = html.escape(contact.full_name or "Người thân")
        phone_btn_html = f"""
        <a href="tel:{safe_phone}" class="w-full bg-emerald-600 hover:bg-emerald-700 active:scale-95 text-white font-bold text-xl py-5 rounded-2xl shadow-lg flex items-center justify-center space-x-3 transition">
            <i class="fa-solid fa-phone-volume text-2xl"></i>
            <span>GỌI NGƯỜI NHÀ ({safe_contact_name}: {safe_phone})</span>
        </a>
        """
    else:
        phone_btn_html = """
        <div class="p-4 bg-slate-200 rounded-2xl text-center text-slate-700 font-bold">
            <i class="fa-solid fa-phone-slash mr-2"></i> Chưa cập nhật số điện thoại người nhà
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Thẻ Cứu Hộ Y Tế - {safe_name}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-slate-100 font-sans p-4 max-w-lg mx-auto min-h-screen">
    <div class="bg-rose-600 text-white p-5 rounded-3xl text-center shadow-xl mb-4">
        <i class="fa-solid fa-triangle-exclamation text-4xl mb-2"></i>
        <h1 class="text-2xl font-black uppercase tracking-wider">Thẻ Cứu Hộ Y Tế Khẩn Cấp</h1>
        <p class="text-sm opacity-90">Hệ thống trợ lý AnTam AI - Hỗ trợ người cao tuổi thất lạc / cần giúp đỡ</p>
    </div>

    <div class="bg-white rounded-3xl p-6 shadow-md border border-slate-200 mb-4 space-y-4">
        <div>
            <span class="text-xs font-bold text-slate-500 uppercase tracking-wider">Họ và tên</span>
            <p class="text-2xl font-extrabold text-slate-900">{safe_name} ({safe_birth_year})</p>
        </div>
        <div>
            <span class="text-xs font-bold text-slate-500 uppercase tracking-wider">Địa chỉ nhà</span>
            <p class="text-lg text-slate-800">{safe_address}</p>
        </div>
        <div class="p-3.5 bg-amber-50 rounded-2xl border border-amber-200">
            <span class="text-xs font-bold text-amber-800 uppercase tracking-wider">Bệnh nền cần lưu ý</span>
            <p class="text-base font-semibold text-amber-950 mt-0.5">{safe_history}</p>
        </div>
        <div class="p-3.5 bg-rose-50 rounded-2xl border border-rose-300">
            <span class="text-xs font-bold text-rose-800 uppercase tracking-wider">⚠️ DỊ ỨNG THUỐC ĐẶC BIỆT</span>
            <p class="text-lg font-black text-rose-700 mt-0.5">{safe_allergies}</p>
        </div>
    </div>

    <!-- HÀNH ĐỘNG CỨU HỘ: GỌI ĐIỆN & PHÁT TÍN HIỆU CỨU HỘ CÓ TỌA ĐỘ GPS -->
    <div class="space-y-3">
        <button onclick="sendEmergencyAlert()" id="btnAlert" class="w-full bg-rose-600 hover:bg-rose-700 active:scale-95 text-white font-bold text-xl py-5 rounded-2xl shadow-xl flex items-center justify-center space-x-3 transition">
            <i class="fa-solid fa-bell text-2xl"></i>
            <span id="btnAlertText">TÔI ĐANG Ở CẠNH CỤ - BÁO CHO GIA ĐÌNH NGAY</span>
        </button>

        {phone_btn_html}

        <a href="tel:115" class="w-full bg-red-700 hover:bg-red-800 active:scale-95 text-white font-bold text-xl py-5 rounded-2xl shadow-lg flex items-center justify-center space-x-3 transition">
            <i class="fa-solid fa-truck-medical text-2xl"></i>
            <span>GỌI CẤP CỨU 115</span>
        </a>
    </div>

    <div id="alertFeedback" class="hidden mt-4 p-4 rounded-2xl text-center font-bold"></div>

    <p class="text-center text-xs text-slate-500 mt-6">
        Dữ liệu được bảo trợ bởi Hệ sinh thái Trợ lý AnTam AI
    </p>

    <script>
        async function sendEmergencyAlert() {{
            const btn = document.getElementById('btnAlert');
            const btnText = document.getElementById('btnAlertText');
            const feedback = document.getElementById('alertFeedback');
            btn.disabled = true;
            btnText.innerText = "ĐANG GỬI VỊ TRÍ CỨU HỘ...";

            let coords = null;
            if (navigator.geolocation) {{
                try {{
                    const pos = await new Promise((resolve, reject) => {{
                        navigator.geolocation.getCurrentPosition(resolve, reject, {{ timeout: 5000 }});
                    }});
                    coords = {{ latitude: pos.coords.latitude, longitude: pos.coords.longitude }};
                }} catch (e) {{
                    console.log("GPS denied or timeout");
                }}
            }}

            try {{
                const res = await fetch('/qr/{token}/alert', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(coords || {{}})
                }});
                const data = await res.json();
                feedback.className = "mt-4 p-4 rounded-2xl text-center font-bold bg-emerald-100 text-emerald-800 border border-emerald-300";
                feedback.innerText = "✅ " + data.message;
                feedback.classList.remove('hidden');
                btnText.innerText = "ĐÃ GỬI TÍN HIỆU CỨU HỘ THÀNH CÔNG";
            }} catch (err) {{
                feedback.className = "mt-4 p-4 rounded-2xl text-center font-bold bg-rose-100 text-rose-800 border border-rose-300";
                feedback.innerText = "Không thể gửi tín hiệu cứu hộ. Vui lòng gọi điện trực tiếp thoại cho người nhà hoặc 115!";
                feedback.classList.remove('hidden');
                btn.disabled = false;
                btnText.innerText = "THỬ LẠI GỬI TÍN HIỆU CỨU HỘ";
            }}
        }}
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.post("/qr/{token}/alert")
async def trigger_emergency_qr_alert(
    token: str,
    req: schemas.QrAlertRequest = None,
    db: Session = Depends(get_db),
):
    """
    Endpoint chủ động ghi nhận tín hiệu người đi đường bấm nút báo cho gia đình,
    kèm theo tọa độ GPS định vị chính xác vị trí cụ ngoài đường.
    """
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.emergency_qr_token == token)
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Mã cứu hộ không tồn tại hoặc đã hết hiệu lực",
        )

    now = models.get_vn_now()
    now_str = now.strftime("%H:%M - %d/%m")

    # M5: Dedup PUBLIC_QR_SCAN trong 10 phút (tương tự VITAL_CRITICAL)
    ten_mins_ago = now - timedelta(minutes=10)
    recent_qr_sos = (
        db.query(models.HealthAndSOSLog)
        .filter(
            models.HealthAndSOSLog.elderly_id == profile.id,
            models.HealthAndSOSLog.trigger_source == "PUBLIC_QR_SCAN",
            models.HealthAndSOSLog.triggered_at >= ten_mins_ago,
        )
        .first()
    )
    if recent_qr_sos:
        return {
            "success": True,
            "message": f"Tín hiệu cứu hộ đã được gửi trước đó, gia đình Cụ {profile.full_name} đã được thông báo rồi ạ!",
        }

    location_desc = "Ngoài đường (Người đi đường quét mã QR)"
    if req and req.latitude and req.longitude:
        location_desc = f"Ngoài đường (Tọa độ GPS: {req.latitude:.5f}, {req.longitude:.5f})"

    sos_entry = models.HealthAndSOSLog(
        elderly_id=profile.id,
        trigger_source="PUBLIC_QR_SCAN",
        escalation_status="BROADCASTED_FAMILY",
        location_note=location_desc,
        triggered_at=now,
    )
    db.add(sos_entry)
    db.commit()

    await manager.broadcast(
        {
            "event": "SOS_ALERT",
            "elderly_id": profile.id,
            "full_name": profile.full_name,
            "trigger_source": "PUBLIC_QR_SCAN",
            "location": location_desc,
            "time": now_str,
            "message": f"⚠️ CẢNH BÁO CỨU HỘ: Có người đi đường đang ở cạnh Cụ {profile.full_name} và vừa gửi tín hiệu yêu cầu hỗ trợ ({location_desc})!",
        },
        elderly_id=profile.id,
    )

    dispatch_family_alert(
        elderly_id=profile.id,
        title=f"🚨 CỨU HỘ NGOÀI ĐƯỜNG: Cụ {profile.full_name}",
        message=f"Người đi đường vừa gửi tín hiệu yêu cầu hỗ trợ Cụ {profile.full_name}. Vị trí: {location_desc}",
        alert_type="PUBLIC_QR_SCAN",
        db=db,
        location=location_desc,
    )

    return {
        "success": True,
        "message": f"Đã gửi thông báo cứu hộ khẩn cấp tới gia đình Cụ {profile.full_name} thành công!",
    }


# ========================================================
# 4. TỰ ĐỘNG LẬP LỊCH NHẮC THUỐC (BACKGROUND SCHEDULER)
# ========================================================


async def check_medication_schedules():
    """
    Tự động quét lịch thuốc và phát nhắc nhở thời gian thực (Rule-based Hai lượt):
    - Lượt 1: Quét các cữ thuốc khớp giờ hiện tại (current_hm) để phát Lần nhắc 1 (REMINDED_1).
    - Lượt 2: Quét toàn bộ các cữ thuốc hôm nay chưa uống (status: REMINDED_1, REMINDED_2, REMINDED_3)
              để leo thang Lần 2 (sau 5p), Lần 3 (sau 10p) và Bỏ lỡ leo thang (MISSED_ESCALATED sau 20p).
    """
    db = SessionLocal()
    try:
        now = models.get_vn_now()
        current_hm = now.strftime("%H:%M")
        today_start = datetime.combine(now.date(), datetime.min.time())

        # LƯỢT 1: Bắt đầu cữ thuốc mới khớp với phút hiện tại
        active_meds = (
            db.query(models.MedicationSchedule)
            .filter(
                models.MedicationSchedule.is_active == True,
                models.MedicationSchedule.verified_by_caregiver == True,
            )
            .all()
        )

        for med in active_meds:
            if not med.schedule_times or current_hm not in med.schedule_times:
                continue

            existing_log = (
                db.query(models.MedicationLog)
                .filter(
                    models.MedicationLog.medication_id == med.id,
                    models.MedicationLog.scheduled_time >= today_start,
                )
                .order_by(models.MedicationLog.scheduled_time.desc())
                .first()
            )

            if not existing_log:
                new_log = models.MedicationLog(
                    medication_id=med.id,
                    elderly_id=med.elderly_id,
                    scheduled_time=now,
                    status="REMINDED_1",
                    reminder_count=1,
                )
                db.add(new_log)
                db.commit()

                prompt = f"Dạ thưa cụ, đã đến giờ uống thuốc {med.medicine_name} rồi ạ! Cụ uống {med.dosage} {med.pill_color_shape} giúp con nhé ạ!"
                await manager.broadcast(
                    {
                        "event": "MEDICATION_REMINDER",
                        "elderly_id": med.elderly_id,
                        "medication_id": med.id,
                        "medicine_name": med.medicine_name,
                        "dosage": med.dosage,
                        "pill_desc": med.pill_color_shape,
                        "time": current_hm,
                        "reminder_count": 1,
                        "speech_prompt": prompt,
                    },
                    elderly_id=med.elderly_id,
                )

        # LƯỢT 2: Theo dõi và leo thang các đơn thuốc đang chờ cụ uống
        open_logs = (
            db.query(models.MedicationLog)
            .filter(
                models.MedicationLog.scheduled_time >= today_start,
                models.MedicationLog.status.in_(
                    ["REMINDED_1", "REMINDED_2", "REMINDED_3"]
                ),
            )
            .all()
        )

        for log in open_logs:
            med = (
                db.query(models.MedicationSchedule)
                .filter(models.MedicationSchedule.id == log.medication_id)
                .first()
            )
            if not med:
                continue

            diff_mins = (now - log.scheduled_time).total_seconds() / 60.0

            if log.reminder_count == 1 and diff_mins >= 5.0:
                log.reminder_count = 2
                log.status = "REMINDED_2"
                db.commit()

                prompt = f"Dạ thưa cụ, con nhắc cụ lần 2 uống thuốc {med.medicine_name} ạ! Cụ uống ngay giúp con nhé!"
                await manager.broadcast(
                    {
                        "event": "MEDICATION_REMINDER",
                        "elderly_id": med.elderly_id,
                        "medication_id": med.id,
                        "medicine_name": med.medicine_name,
                        "dosage": med.dosage,
                        "pill_desc": med.pill_color_shape,
                        "time": current_hm,
                        "reminder_count": 2,
                        "speech_prompt": prompt,
                    },
                    elderly_id=med.elderly_id,
                )

            elif log.reminder_count == 2 and diff_mins >= 10.0:
                log.reminder_count = 3
                log.status = "REMINDED_3"
                db.commit()

                prompt = f"Dạ thưa cụ, con nhắc cụ lần 3 uống thuốc {med.medicine_name} ạ! Cụ uống ngay giúp con nhé!"
                await manager.broadcast(
                    {
                        "event": "MEDICATION_REMINDER",
                        "elderly_id": med.elderly_id,
                        "medication_id": med.id,
                        "medicine_name": med.medicine_name,
                        "dosage": med.dosage,
                        "pill_desc": med.pill_color_shape,
                        "time": current_hm,
                        "reminder_count": 3,
                        "speech_prompt": prompt,
                    },
                    elderly_id=med.elderly_id,
                )

            elif log.reminder_count >= 3 and diff_mins >= 20.0:
                log.status = "MISSED_ESCALATED"

                # Tránh trùng lặp báo động bỏ lỡ thuốc
                existing_sos = (
                    db.query(models.HealthAndSOSLog)
                    .filter(
                        models.HealthAndSOSLog.elderly_id == med.elderly_id,
                        models.HealthAndSOSLog.trigger_source
                        == "DEADMAN_MISSED",
                        models.HealthAndSOSLog.triggered_at >= today_start,
                        models.HealthAndSOSLog.location_note.like(
                            f"%{med.medicine_name}%"
                        ),
                    )
                    .first()
                )

                if not existing_sos:
                    sos_log = models.HealthAndSOSLog(
                        elderly_id=med.elderly_id,
                        trigger_source="DEADMAN_MISSED",
                        escalation_status="BROADCASTED_FAMILY",
                        location_note=f"Bỏ lỡ cữ thuốc {med.medicine_name} sau 3 lần nhắc",
                        triggered_at=now,
                    )
                    db.add(sos_log)

                db.commit()

                await manager.broadcast(
                    {
                        "event": "MEDICATION_MISSED_ALERT",
                        "elderly_id": med.elderly_id,
                        "medication_id": med.id,
                        "medicine_name": med.medicine_name,
                        "scheduled_time": log.scheduled_time.strftime("%H:%M"),
                        "time": current_hm,
                        "message": f"CẢNH BÁO: Cụ chưa uống thuốc {med.medicine_name} sau 3 lần nhắc nhở!",
                    },
                    elderly_id=med.elderly_id,
                )

                dispatch_family_alert(
                    elderly_id=med.elderly_id,
                    title=f"⚠️ CẢNH BÁO: Bỏ lỡ cữ thuốc {med.medicine_name}",
                    message=f"Cụ chưa uống thuốc {med.medicine_name} ({log.scheduled_time.strftime('%H:%M')}) sau 3 lần phát loa nhắc nhở!",
                    alert_type="MEDICATION_MISSED",
                    db=db,
                )

    except Exception as e:
        print(f"Error in scheduler: {e}")
    finally:
        db.close()


async def background_scheduler_worker():
    """Vòng lặp ngầm quét lịch thuốc mỗi 30 giây"""
    while True:
        try:
            await check_medication_schedules()
        except Exception as e:
            print("Background scheduler exception:", e)
        await asyncio.sleep(30)


# ========================================================
# 5. API CON CÁI (FAMILY / CAREGIVER DASHBOARD & ĐƠN THUỐC)
# ========================================================


@app.post(
    "/api/family/medications/create", response_model=schemas.MedicationResponse
)
def create_medication_schedule(
    med_in: schemas.MedicationCreate, db: Session = Depends(get_db)
):
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.id == med_in.elderly_id)
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=404, detail="Không tìm thấy hồ sơ người cao tuổi"
        )

    # Đơn thuốc do chính người nhà kê tạo sẽ tự động được xác thực (verified_by_caregiver = True)
    med = models.MedicationSchedule(
        elderly_id=med_in.elderly_id,
        medicine_name=med_in.medicine_name,
        pill_color_shape=med_in.pill_color_shape,
        dosage=med_in.dosage,
        instructions=med_in.instructions,
        schedule_times=med_in.schedule_times,
        verified_by_caregiver=True,
    )
    db.add(med)
    db.commit()
    db.refresh(med)
    return med


@app.post("/api/family/{elderly_id}/medications/{med_id}/verify")
def verify_medication_schedule(elderly_id: str, med_id: str, db: Session = Depends(get_db)):
    med = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.id == med_id,
            models.MedicationSchedule.elderly_id == elderly_id,  # IDOR fix C4
        )
        .first()
    )
    if not med:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn thuốc")

    med.verified_by_caregiver = True
    db.commit()
    return {
        "success": True,
        "message": f"Đã xác nhận đơn thuốc {med.medicine_name} cho người cao tuổi.",
    }


@app.post("/api/family/{elderly_id}/medications/{med_id}/toggle-active")
def toggle_medication_active(elderly_id: str, med_id: str, db: Session = Depends(get_db)):
    med = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.id == med_id,
            models.MedicationSchedule.elderly_id == elderly_id,  # IDOR fix C4
        )
        .first()
    )
    if not med:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn thuốc")

    med.is_active = not med.is_active
    db.commit()
    status_label = "kích hoạt dùng" if med.is_active else "tạm dừng"
    return {
        "success": True,
        "is_active": med.is_active,
        "message": f"Đã chuyển đơn thuốc {med.medicine_name} sang trạng thái {status_label}.",
    }


@app.get("/api/family/{elderly_id}/medications")
def get_family_medications(elderly_id: str, db: Session = Depends(get_db)):
    # ponytail: lấy toàn bộ danh sách đơn thuốc (gồm cả đang dùng & tạm dừng) cho người nhà quản lý
    meds = (
        db.query(models.MedicationSchedule)
        .filter(models.MedicationSchedule.elderly_id == elderly_id)
        .order_by(models.MedicationSchedule.created_at.desc())
        .all()
    )
    return [
        {
            "id": m.id,
            "medicine_name": m.medicine_name,
            "pill_color_shape": m.pill_color_shape,
            "dosage": m.dosage,
            "instructions": m.instructions,
            "schedule_times": m.schedule_times,
            "verified_by_caregiver": m.verified_by_caregiver,
            "is_active": m.is_active,
        }
        for m in meds
    ]


@app.get(
    "/api/family/{elderly_id}/contacts",
    response_model=List[schemas.EmergencyContactResponse],
)
def get_emergency_contacts(elderly_id: str, db: Session = Depends(get_db)):
    contacts = (
        db.query(models.FamilyMember)
        .filter(models.FamilyMember.elderly_id == elderly_id)
        .order_by(
            models.FamilyMember.is_primary_contact.desc(),
            models.FamilyMember.created_at.asc(),
        )
        .all()
    )
    return contacts


@app.post(
    "/api/family/{elderly_id}/contacts",
    response_model=schemas.EmergencyContactResponse,
)
def add_emergency_contact(
    elderly_id: str,
    contact_in: schemas.EmergencyContactCreate,
    db: Session = Depends(get_db),
):
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.id == elderly_id)
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=404, detail="Không tìm thấy hồ sơ người cao tuổi"
        )

    clean_phone = re.sub(r"\s+", "", contact_in.phone_number)
    if not re.match(r"^0\d{9}$", clean_phone):  # M1: VN mobile = đúng 10 chữ số từ 2018
        raise HTTPException(
            status_code=422,
            detail="Số điện thoại không hợp lệ (Phải bắt đầu bằng số 0 và có 10-11 chữ số).",
        )

    if contact_in.is_primary_contact:
        db.query(models.FamilyMember).filter(
            models.FamilyMember.elderly_id == elderly_id
        ).update({"is_primary_contact": False})

    new_contact = models.FamilyMember(
        elderly_id=elderly_id,
        full_name=contact_in.full_name.strip(),
        phone_number=clean_phone,
        relationship_type=contact_in.relationship_type,
        is_primary_contact=contact_in.is_primary_contact,
    )
    db.add(new_contact)
    db.commit()
    db.refresh(new_contact)
    return new_contact


@app.delete("/api/family/{elderly_id}/contacts/{contact_id}")
def delete_emergency_contact(
    elderly_id: str, contact_id: str, db: Session = Depends(get_db)
):
    contact = (
        db.query(models.FamilyMember)
        .filter(
            models.FamilyMember.id == contact_id,
            models.FamilyMember.elderly_id == elderly_id,
        )
        .first()
    )
    if not contact:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy người liên hệ của người cao tuổi này",
        )

    if contact.is_primary_contact:
        raise HTTPException(
            status_code=400,
            detail="Không thể xóa người liên hệ chính duy nhất. Vui lòng chỉ định người khác làm liên hệ chính trước.",
        )

    db.delete(contact)
    db.commit()
    return {
        "success": True,
        "message": f"Đã xóa liên hệ {contact.full_name} khỏi danh bạ khẩn cấp.",
    }


@app.get("/api/family/summary/{elderly_id}")
def get_family_summary(elderly_id: str, db: Session = Depends(get_db)):
    profile = (
        db.query(models.ElderlyProfile)
        .filter(models.ElderlyProfile.id == elderly_id)
        .first()
    )
    if not profile:
        raise HTTPException(
            status_code=404, detail="Không tìm thấy hồ sơ người cao tuổi"
        )

    now = models.get_vn_now()
    today_start = datetime.combine(now.date(), datetime.min.time())

    active_meds = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.elderly_id == elderly_id,
            models.MedicationSchedule.is_active == True,
            models.MedicationSchedule.verified_by_caregiver == True,
        )
        .all()
    )
    # ponytail: đếm tổng cữ theo schedule_times array, upgrade to recurrence rule khi có thuốc cách nhật
    total_doses = sum(len(m.schedule_times) if isinstance(m.schedule_times, list) else 1 for m in active_meds)

    taken_count = (
        db.query(models.MedicationLog)
        .filter(
            models.MedicationLog.elderly_id == elderly_id,
            models.MedicationLog.scheduled_time >= today_start,
            models.MedicationLog.status == "TAKEN",
        )
        .count()
    )

    latest_vital = (
        db.query(models.VitalSign)
        .filter(models.VitalSign.elderly_id == elderly_id)
        .order_by(models.VitalSign.measured_at.desc())
        .first()
    )

    compliance = (
        round((taken_count / total_doses * 100), 1) if total_doses > 0 else 100.0
    )

    return {
        "profile": {
            "id": profile.id,
            "full_name": profile.full_name,
            "birth_year": profile.birth_year,
            "medical_history": profile.medical_history,
            "pairing_code": profile.pairing_code,
        },
        "medication_stats": {
            "total_today": total_doses,
            "taken_today": taken_count,
            "compliance_rate": compliance,
        },
        "latest_vital": {
            "systolic": latest_vital.systolic if latest_vital else None,
            "diastolic": latest_vital.diastolic if latest_vital else None,
            "heart_rate": latest_vital.heart_rate if latest_vital else None,
            "category": latest_vital.health_category if latest_vital else None,
            "time": (
                latest_vital.measured_at.strftime("%H:%M %d/%m")
                if latest_vital
                else None
            ),
        },
    }


@app.get("/api/family/{elderly_id}/history")
def get_family_history(elderly_id: str, days: int = 7, db: Session = Depends(get_db)):
    # ponytail: giới hạn 30 ngày tránh query quét bảng lớn, upgrade to partition khi DB > 100k rows
    days = min(max(days, 1), 30)
    now = models.get_vn_now()

    active_meds = (
        db.query(models.MedicationSchedule)
        .filter(
            models.MedicationSchedule.elderly_id == elderly_id,
            models.MedicationSchedule.is_active == True,
            models.MedicationSchedule.verified_by_caregiver == True,
        )
        .all()
    )
    daily_expected_doses = sum(len(m.schedule_times) if isinstance(m.schedule_times, list) else 1 for m in active_meds)

    history = []
    for i in range(days - 1, -1, -1):
        target_date = (now - timedelta(days=i)).date()
        day_start = datetime.combine(target_date, datetime.min.time())
        day_end = datetime.combine(target_date, datetime.max.time())

        taken_count = (
            db.query(models.MedicationLog)
            .filter(
                models.MedicationLog.elderly_id == elderly_id,
                models.MedicationLog.scheduled_time >= day_start,
                models.MedicationLog.scheduled_time <= day_end,
                models.MedicationLog.status == "TAKEN",
            )
            .count()
        )

        vital = (
            db.query(models.VitalSign)
            .filter(
                models.VitalSign.elderly_id == elderly_id,
                models.VitalSign.measured_at >= day_start,
                models.VitalSign.measured_at <= day_end,
            )
            .order_by(models.VitalSign.measured_at.desc())
            .first()
        )

        comp = round((taken_count / daily_expected_doses * 100), 1) if daily_expected_doses > 0 else 100.0
        history.append({
            "date": target_date.strftime("%d/%m"),
            "full_date": target_date.isoformat(),
            "total_doses": daily_expected_doses,
            "taken_doses": taken_count,
            "compliance_rate": comp,
            "systolic": vital.systolic if vital else None,
            "diastolic": vital.diastolic if vital else None,
            "heart_rate": vital.heart_rate if vital else None,
        })
    return history


@app.get("/api/family/{elderly_id}/notifications")
def get_family_notifications(elderly_id: str):
    # ponytail: trả về danh sách cảnh báo đã dispatch trong bộ nhớ, upgrade to persistent table khi kết nối dịch vụ ngoài
    return [n for n in reversed(DISPATCHED_NOTIFICATIONS) if n.get("elderly_id") == elderly_id]


# ========================================================
# 6. SEED DATA KHỞI ĐỘNG & BẬT BACKGROUND WORKER
# ========================================================
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
            emergency_qr_token="qr_lan_1952",
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
            schedule_times=["08:00"],
            verified_by_caregiver=True,
        )
        thuoc_2 = models.MedicationSchedule(
            elderly_id=cụ_lan.id,
            medicine_name="Glucophage 500mg",
            pill_color_shape="Viên con nhộng trắng",
            dosage="1 viên",
            instructions="Uống sau ăn tối",
            schedule_times=["19:30"],
            verified_by_caregiver=True,
        )
        db.add_all([thuoc_1, thuoc_2])

        now = models.get_vn_now()
        v1 = models.VitalSign(
            elderly_id=cụ_lan.id,
            systolic=125,
            diastolic=80,
            heart_rate=76,
            health_category="NORMAL",
            measured_at=now - timedelta(days=2),
        )
        v2 = models.VitalSign(
            elderly_id=cụ_lan.id,
            systolic=132,
            diastolic=84,
            heart_rate=78,
            health_category="HIGH_NORMAL",
            measured_at=now - timedelta(days=1),
        )
        v3 = models.VitalSign(
            elderly_id=cụ_lan.id,
            systolic=135,
            diastolic=85,
            heart_rate=75,
            health_category="HIGH_NORMAL",
            measured_at=now - timedelta(hours=3),
        )
        db.add_all([v1, v2, v3])

        db.commit()

    # L3 fix: Chỉ verify những đơn thuốc chưa được caregiver xem xét (không override trạng thái đã set)
    db.query(models.MedicationSchedule).filter(
        models.MedicationSchedule.elderly_id == "lan_1952",
        models.MedicationSchedule.verified_by_caregiver == False,
    ).update({"verified_by_caregiver": True})
    db.commit()

    # Đảm bảo có thông tin người nhà liên hệ khẩn cấp
    con_trai = (
        db.query(models.FamilyMember)
        .filter(models.FamilyMember.elderly_id == "lan_1952")
        .first()
    )
    if not con_trai:
        con_trai = models.FamilyMember(
            elderly_id="lan_1952",
            full_name="Nguyễn Văn Hùng",
            phone_number="0912345678",
            relationship_type="CON_TRAI",
            is_primary_contact=True,
        )
        db.add(con_trai)
        db.commit()

    db.close()


@app.on_event("startup")
async def on_app_startup():
    startup_seed_data()
    asyncio.create_task(background_scheduler_worker())
