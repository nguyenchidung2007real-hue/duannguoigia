"""
Dịch vụ kiểm tra và phân loại chỉ số huyết áp theo chuẩn Y tế (Rule-based Guardrail) - Bản nâng cấp V3.
- Xử lý số lẻ "rưỡi", dấu phẩy thập phân và đọc rút gọn (13 rưỡi -> 135, 8 rưỡi -> 85).
- Thang đo Mức độ nghiêm trọng (Severity Score: 0 -> 4) đảm bảo không bị đè mất cảnh báo Hạ huyết áp.
- Phát hiện kẹp huyết áp nguy hiểm (ví dụ 155/55: Vừa tăng tâm thu vừa hạ tâm trương).
"""
import re

def normalize_bp_value(val: float) -> int:
    """
    Chuẩn hóa số đọc rút gọn:
    - 13 -> 130
    - 13.5 -> 135
    - 8 -> 80
    - 8.5 -> 85
    - 120 -> 120
    """
    if val is None:
        return None
    # Nếu giá trị nằm trong khoảng từ 5.0 đến 25.0 -> nhân 10 và làm tròn về số nguyên
    if 5.0 <= val <= 25.0:
        return int(round(val * 10.0))
    return int(round(val))

def preprocess_voice_text(text: str) -> str:
    """
    Tiền xử lý văn bản giọng nói tiếng Việt:
    - "13 rưỡi" -> "13.5"
    - "mười ba rưỡi" -> "13.5"
    - Đổi dấu phẩy thành dấu chấm ("13,5" -> "13.5")
    - Xử lý khoảng trắng lẻ ("13 5" -> "13.5")
    """
    t = text.lower()
    # Đổi chữ số cơ bản nếu STT trả về chữ
    word_to_num = {
        "mười một": "11", "mười hai": "12", "mười ba": "13", "mười bốn": "14", 
        "mười lăm": "15", "mười sáu": "16", "mười bảy": "17", "mười tám": "18", 
        "mười chín": "19", "hai mươi": "20", "tám": "8", "chín": "9", "mười": "10",
        "bảy": "7", "sáu": "6"
    }
    for w, n in word_to_num.items():
        t = re.sub(rf'\b{w}\b', n, t)

    # Đổi "rưỡi" thành ".5"
    t = re.sub(r'(\d+)\s*rưỡi', r'\1.5', t)
    # Đổi dấu phẩy thập phân thành dấu chấm ("13,5" -> "13.5")
    t = re.sub(r'(\d+),(\d+)', r'\1.\2', t)
    # Đổi số cách số ("13 5" -> "13.5") nếu là phần thập phân
    t = re.sub(r'(\b\d{1,2})\s+([1-9]\b)', r'\1.\2', t)
    return t

def evaluate_blood_pressure(systolic: float, diastolic: float, pulse: float = None) -> dict:
    """
    Phân loại huyết áp theo Thang đo Mức độ nghiêm trọng (Severity Score):
    - Level 0: Bình thường (90-119 / 60-79)
    - Level 1: Tiền tăng huyết áp (120-139 / 80-89)
    - Level 2: Tăng HA độ 1 (140-159 / 90-99)
    - Level 3: Hạ huyết áp (< 90 hoặc < 60) HOẶC Tăng HA độ 2 (160-179 / 100-109)
    - Level 4: Khẩn cấp nguy hiểm (>= 180 / >= 110 HOẶC tụt nặng < 70 / < 40)
    """
    systolic = normalize_bp_value(systolic)
    diastolic = normalize_bp_value(diastolic)

    if systolic is None or diastolic is None:
        return {
            "category": "INVALID",
            "severity_level": 0,
            "title": "Thiếu chỉ số",
            "systolic": systolic,
            "diastolic": diastolic,
            "advice": "Dạ con chưa nhận đủ hai chỉ số huyết áp. Cụ đọc lại ví dụ '130 trên 80' hoặc '13 trên 8' giúp con nhé ạ!"
        }

    if systolic <= diastolic:
        return {
            "category": "INVALID",
            "severity_level": 0,
            "title": "Chỉ số không hợp lệ",
            "systolic": systolic,
            "diastolic": diastolic,
            "advice": "Chỉ số huyết áp tâm thu (số trên) phải lớn hơn tâm trương (số dưới). Cụ đo lại cẩn thận giúp con nhé ạ!"
        }

    # 1. Đánh giá Mức độ nghiêm trọng Tâm thu
    if systolic >= 180:
        sys_cat, sys_sev = "CRITICAL_HIGH", 4
    elif systolic >= 160:
        sys_cat, sys_sev = "STAGE_2", 3
    elif systolic >= 140:
        sys_cat, sys_sev = "STAGE_1", 2
    elif systolic >= 120:
        sys_cat, sys_sev = "HIGH_NORMAL", 1
    elif systolic >= 90:
        sys_cat, sys_sev = "NORMAL", 0
    elif systolic < 70:
        sys_cat, sys_sev = "CRITICAL_LOW", 4 # Tụt huyết áp nặng nguy kịch
    else:
        sys_cat, sys_sev = "LOW_BP", 3 # Hạ huyết áp nguy cơ ngất ngã

    # 2. Đánh giá Mức độ nghiêm trọng Tâm trương
    if diastolic >= 110:
        dia_cat, dia_sev = "CRITICAL_HIGH", 4
    elif diastolic >= 100:
        dia_cat, dia_sev = "STAGE_2", 3
    elif diastolic >= 90:
        dia_cat, dia_sev = "STAGE_1", 2
    elif diastolic >= 80:
        dia_cat, dia_sev = "HIGH_NORMAL", 1
    elif diastolic >= 60:
        dia_cat, dia_sev = "NORMAL", 0
    elif diastolic < 40:
        dia_cat, dia_sev = "CRITICAL_LOW", 4
    else:
        dia_cat, dia_sev = "LOW_BP", 3

    # 3. Lấy Severity Score cao nhất
    max_severity = max(sys_sev, dia_sev)

    # 4. Kiểm tra trường hợp đặc biệt: Vừa tăng tâm thu vừa hạ tâm trương (Lệch pha nguy hiểm)
    is_mixed_danger = (systolic >= 140 and diastolic < 60)
    is_isolated_systolic = (systolic >= 140 and 60 <= diastolic < 90)

    if max_severity == 4:
        title = "Huyết áp nguy kịch khẩn cấp"
        advice = "Dạ thưa cụ, chỉ số huyết áp của cụ đang ở mức nguy hiểm ạ! Cụ hãy ngồi tựa lưng hoặc nằm nghỉ ngay tại chỗ, không đi lại. Con đang gửi báo động khẩn cấp cho gia đình mình ngay đây ạ!"
        cat = "CRITICAL_HIGH" if systolic >= 180 or diastolic >= 110 else "CRITICAL_LOW"
    elif is_mixed_danger:
        title = "Tăng tâm thu kèm tụt tâm trương"
        advice = "Dạ thưa cụ, huyết áp của cụ có sự chênh lệch lớn giữa hai số (số trên cao nhưng số dưới thấp). Cụ hãy nằm nghỉ ngơi tại giường, tránh đứng dậy đột ngột và báo cho con cháu biết nhé ạ!"
        cat = "MIXED_DANGER"
    elif sys_sev == 3 or dia_sev == 3:
        if sys_cat == "LOW_BP" or dia_cat == "LOW_BP":
            title = "Hạ huyết áp (Huyết áp thấp nguy hiểm)"
            advice = "Dạ thưa cụ, huyết áp hôm nay của cụ đang khá thấp ạ! Cụ hãy ngồi nghỉ ngơi hoặc nằm đầu thấp, không đứng dậy đột ngột để tránh choáng ngã nhé. Cụ uống một cốc nước ấm hoặc trà gừng ấm giúp con ạ!"
            cat = "HYPOTENSION_LOW"
        else:
            title = "Tăng huyết áp độ 2"
            advice = "Dạ thưa cụ, huyết áp hôm nay của cụ hơi cao ạ. Cụ kiểm tra lại xem sáng nay mình đã uống thuốc huyết áp theo cữ chưa nhé. Cụ ngồi nghỉ ngơi thả lỏng người giúp con ạ!"
            cat = "HYPERTENSION_STAGE_2"
    elif max_severity == 2:
        note = "tâm thu đơn độc " if is_isolated_systolic else ""
        title = f"Tăng huyết áp {note}độ 1"
        advice = "Dạ thưa cụ, huyết áp hôm nay hơi cao nhẹ một chút so với mức chuẩn. Cụ nhớ duy trì chế độ ăn nhạt, tránh lo lắng và nghỉ ngơi nhé ạ!"
        cat = "HYPERTENSION_STAGE_1"
    elif max_severity == 1:
        title = "Huyết áp bình thường cao"
        advice = "Dạ thưa cụ, chỉ số huyết áp của cụ ở mức an toàn ạ. Cụ duy trì tinh thần vui tươi, sinh hoạt điều độ như thế này nhé ạ!"
        cat = "HIGH_NORMAL"
    else:
        title = "Huyết áp rất đẹp (Chuẩn)"
        advice = "Dạ thưa cụ, chỉ số huyết áp hôm nay của cụ rất đẹp và lý tưởng ạ! Cụ tiếp tục duy trì sức khỏe thật tốt nhé ạ!"
        cat = "NORMAL"

    return {
        "category": cat,
        "severity_level": max_severity,
        "title": title,
        "systolic": systolic,
        "diastolic": diastolic,
        "pulse": pulse,
        "is_emergency": (max_severity == 4),
        "advice": advice
    }
