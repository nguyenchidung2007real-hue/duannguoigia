"""
Dịch vụ kiểm tra và phân loại chỉ số huyết áp theo chuẩn Y tế (Rule-based Guardrail) - Bản nâng cấp V3.2.
- Kiểm tra khoảng sinh lý nghiêm ngặt: Tâm thu (60-260 mmHg), Tâm trương (30-160 mmHg), Nhịp tim (30-220 bpm).
- Xử lý số lẻ "rưỡi", "phẩy", dấu phẩy thập phân và đọc rút gọn (7.0 <= val <= 25.0 nhân 10; tránh nhân sai các số biên).
- Ghép các chữ số rời rạc từ STT (ví dụ "1 2 0" -> 120, "8 0" -> 80), bảo vệ nguyên vẹn "13 8" và "12 7".
- Thang đo Mức độ nghiêm trọng (Severity Score: 0 -> 4):
  + Tách riêng "Huyết áp hơi thấp" (HYPOTENSION_MILD: 80-89 / 50-59) khỏi "Hạ huyết áp nguy hiểm" (HYPOTENSION_LOW: 70-79 / 40-49).
  + Mức nguy kịch (CRITICAL_HIGH >= 180/110 hoặc CRITICAL_LOW < 70/40) bổ sung chỉ dẫn khẩn cấp gọi 115 khi có triệu chứng ngực/thần kinh.
- Đánh giá lâm sàng nhịp tim (Tachycardia / Bradycardia) đi kèm lời dặn thả lỏng.
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
    Chỉ áp dụng nhân 10 đối với khoảng đọc rút gọn y khoa chuẩn xác (7.0 <= val <= 25.0)
    """
    if val is None:
        return None
    if 7.0 <= val <= 25.0:
        return int(round(val * 10.0))
    return int(round(val))

def preprocess_voice_text(text: str) -> str:
    """
    Tiền xử lý văn bản giọng nói tiếng Việt:
    - "1 2 0 trên 80" -> "120 trên 80" (ghép các chữ số rời)
    - "8 0" -> "80"
    - "13 rưỡi", "13 phẩy 5", "13 phẩy năm" -> "13.5"
    - Đổi dấu phẩy thành dấu chấm ("13,5" -> "13.5")
    - Chỉ gộp khi số đằng sau là số 5 ("13 5" -> "13.5"), tuyệt đối KHÔNG gộp "13 8" hoặc "12 7"
    """
    t = text.lower()
    # 1. Đổi chữ số cơ bản nếu STT trả về chữ (Lưu ý: Không đổi 'không' thành '0' để tránh nhầm với từ phủ định 'không có')
    word_to_num = {
        "mười một": "11", "mười hai": "12", "mười ba": "13", "mười bốn": "14", 
        "mười lăm": "15", "mười sáu": "16", "mười bảy": "17", "mười tám": "18", 
        "mười chín": "19", "hai mươi": "20", "tám": "8", "chín": "9", "mười": "10",
        "bảy": "7", "sáu": "6", "năm": "5", "bốn": "4", "ba": "3", "hai": "2", "một": "1"
    }
    for w, n in word_to_num.items():
        t = re.sub(rf'\b{w}\b', n, t)

    # 2. Ghép 3 chữ số rời rạc liên tiếp chỉ khi bắt đầu bằng 1 hoặc 2 (như "1 2 0" -> "120", tránh gộp nhầm "5 8 5")
    t = re.sub(r'\b([12])\s+(\d)\s+(\d)\b', r'\1\2\3', t)
    # 3. Ghép 2 chữ số rời rạc có số 0 ở sau: "8 0" -> "80", "9 0" -> "90"
    t = re.sub(r'\b([1-9])\s+(0\b)', r'\1\2', t)

    # 4. Đổi "rưỡi" và "phẩy" thành ".5"
    t = re.sub(r'(\d+)\s*rưỡi', r'\1.5', t)
    t = re.sub(r'(\d+)\s*phẩy\s*năm\b', r'\1.5', t)
    t = re.sub(r'(\d+)\s*phẩy\s*(\d+)', r'\1.\2', t)
    t = re.sub(r'(\d+),(\d+)', r'\1.\2', t)

    # 5. Chỉ gộp khoảng trắng khi số đằng sau là 5 (Ví dụ: "13 5" -> "13.5", giữ nguyên "13 8")
    t = re.sub(r'(\b(?:1[0-9]|2[0-2]|[6-9]))\s+(5\b)', r'\1.\2', t)
    return t

def evaluate_blood_pressure(systolic: float, diastolic: float, pulse: float = None) -> dict:
    """
    Phân loại huyết áp theo Thang đo Mức độ nghiêm trọng (Severity Score: 0 -> 4)
    kết hợp kiểm tra giới hạn sinh lý và đánh giá nhịp tim.
    """
    systolic = normalize_bp_value(systolic)
    diastolic = normalize_bp_value(diastolic)

    # 1. Kiểm tra thiếu chỉ số (Sử dụng is None để tránh lỗi falsy với số 0)
    if systolic is None or diastolic is None:
        if systolic is not None and diastolic is None:
            missing_msg = f"Dạ con mới chỉ nghe được một chỉ số huyết áp ({systolic}). Cụ đọc lại đủ cả hai số, số trên và số dưới giúp con nhé ạ!"
        elif systolic is None and diastolic is not None:
            missing_msg = f"Dạ con mới chỉ nghe được chỉ số tâm trương ({diastolic}). Cụ đọc lại đủ cả hai số, số trên và số dưới giúp con nhé ạ!"
        else:
            missing_msg = "Dạ con chưa nhận đủ hai chỉ số huyết áp. Cụ đọc lại ví dụ '130 trên 80' hoặc '13 trên 8' giúp con nhé ạ!"

        return {
            "category": "INVALID",
            "severity_level": 0,
            "title": "Thiếu chỉ số",
            "systolic": systolic,
            "diastolic": diastolic,
            "pulse": pulse,
            "is_emergency": False,
            "advice": missing_msg
        }

    # 2. Logic y khoa cơ bản: Số trên phải lớn hơn số dưới
    if systolic <= diastolic:
        return {
            "category": "INVALID",
            "severity_level": 0,
            "title": "Chỉ số không hợp lệ",
            "systolic": systolic,
            "diastolic": diastolic,
            "pulse": pulse,
            "is_emergency": False,
            "advice": "Chỉ số huyết áp tâm thu (số trên) phải lớn hơn tâm trương (số dưới). Cụ đo lại cẩn thận giúp con nhé ạ!"
        }

    # 3. Kiểm tra giới hạn sinh lý người bình thường
    if systolic < 60 or systolic > 260 or diastolic < 30 or diastolic > 160:
        return {
            "category": "INVALID",
            "severity_level": 0,
            "title": "Chỉ số ngoài khoảng sinh lý",
            "systolic": systolic,
            "diastolic": diastolic,
            "pulse": pulse,
            "is_emergency": False,
            "advice": f"Chỉ số huyết áp {systolic}/{diastolic} nằm ngoài giới hạn sinh lý đo lường thông thường (Tâm thu từ 60-260, Tâm trương từ 30-160 mmHg). Cụ kiểm tra lại máy đo và đọc lại giúp con nhé ạ!"
        }

    # 4. Kiểm tra và đánh giá nhịp tim (pulse) nếu có
    pulse_note = ""
    pulse_status = "NORMAL"
    if pulse is not None:
        if pulse < 30 or pulse > 220:
            return {
                "category": "INVALID",
                "severity_level": 0,
                "title": "Nhịp tim không hợp lệ",
                "systolic": systolic,
                "diastolic": diastolic,
                "pulse": pulse,
                "is_emergency": False,
                "advice": f"Chỉ số nhịp tim ({int(pulse)} nhịp/phút) nằm ngoài giới hạn sinh lý đo lường thông thường (30 - 220 nhịp/phút). Cụ kiểm tra lại máy đo và đọc lại giúp con nhé ạ!"
            }
        elif pulse > 120:
            pulse_status = "TACHYCARDIA_HIGH"
            pulse_note = f" Nhịp tim của cụ đang khá nhanh ({int(pulse)} nhịp/phút), cụ chú ý thả lỏng và hít thở thật sâu nhé ạ."
        elif pulse > 100:
            pulse_status = "TACHYCARDIA"
            pulse_note = f" Nhịp tim {int(pulse)} nhịp/phút (hơi nhanh nhẹ)."
        elif pulse < 50:
            pulse_status = "BRADYCARDIA_LOW"
            pulse_note = f" Nhịp tim của cụ đang khá chậm ({int(pulse)} nhịp/phút), cụ chú ý nghỉ ngơi nhé ạ."
        elif pulse < 60:
            pulse_status = "BRADYCARDIA"
            pulse_note = f" Nhịp tim {int(pulse)} nhịp/phút (hơi chậm nhẹ)."

    # 5. Đánh giá Mức độ nghiêm trọng Tâm thu
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
    elif systolic >= 80:
        sys_cat, sys_sev = "MILD_LOW", 1
    elif systolic >= 70:
        sys_cat, sys_sev = "LOW_BP", 3
    else:
        sys_cat, sys_sev = "CRITICAL_LOW", 4

    # 6. Đánh giá Mức độ nghiêm trọng Tâm trương
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
    elif diastolic >= 50:
        dia_cat, dia_sev = "MILD_LOW", 1
    elif diastolic >= 40:
        dia_cat, dia_sev = "LOW_BP", 3
    else:
        dia_cat, dia_sev = "CRITICAL_LOW", 4

    # 7. Lấy Severity Score cao nhất
    max_severity = max(sys_sev, dia_sev)

    # 8. Bắt các phân nhánh đặc biệt
    is_mixed_danger = (systolic >= 140 and diastolic < 60)
    is_isolated_systolic = (systolic >= 140 and 60 <= diastolic < 90)

    if max_severity == 4:
        if systolic >= 180 or diastolic >= 110:
            cat = "CRITICAL_HIGH"
            title = "Cơn tăng huyết áp nguy kịch khẩn cấp"
            advice = "Dạ thưa cụ, chỉ số huyết áp của cụ đang ở mức rất cao nguy hiểm ạ! Cụ hãy ngồi tựa lưng hoặc nằm nghỉ ngay tại chỗ, thả lỏng người, không đi lại. Nếu cảm thấy đau tức ngực, khó thở, choáng váng hoặc tê yếu tay chân, gia đình cần gọi ngay cấp cứu 115. Con đang phát chuông báo động khẩn cấp tới con cháu ngay đây ạ!"
        else:
            cat = "CRITICAL_LOW"
            title = "Tụt huyết áp nguy kịch khẩn cấp"
            advice = "Dạ thưa cụ, chỉ số huyết áp của cụ đang tụt rất thấp ở mức nguy hiểm ạ! Cụ hãy nằm nghỉ ngay tại chỗ với tư thế kê chân cao hơn đầu một chút, không đứng dậy đi lại. Nếu có cảm giác hoa mắt, vã mồ hôi, khó thở hoặc choáng ngất, gia đình cần gọi ngay cấp cứu 115. Con đang gửi chuông báo động khẩn cấp cho con cháu ngay đây ạ!"
    elif is_mixed_danger:
        title = "Tăng tâm thu kèm tụt tâm trương"
        advice = "Dạ thưa cụ, huyết áp của cụ có sự chênh lệch lớn giữa hai số (số trên cao nhưng số dưới thấp). Cụ hãy nằm nghỉ ngơi tại giường, tránh đứng dậy đột ngột và báo cho con cháu biết nhé ạ!"
        cat = "MIXED_DANGER"
        max_severity = max(max_severity, 3)
    elif sys_sev == 3 or dia_sev == 3:
        if sys_cat == "LOW_BP" or dia_cat == "LOW_BP":
            title = "Hạ huyết áp (Huyết áp thấp nguy hiểm)"
            advice = "Dạ thưa cụ, huyết áp hôm nay của cụ đang khá thấp ạ. Cụ hãy ngồi nghỉ ngơi hoặc nằm đầu bằng, tránh đứng dậy đột ngột để chống choáng ngã. Cụ uống một cốc nước ấm hoặc ăn nhẹ một chút giúp con nhé ạ!"
            cat = "HYPOTENSION_LOW"
        else:
            title = "Tăng huyết áp độ 2"
            advice = "Dạ thưa cụ, huyết áp hôm nay của cụ hơi cao ạ. Cụ kiểm tra lại xem sáng nay mình đã uống thuốc huyết áp theo cữ chưa nhé. Cụ ngồi nghỉ ngơi thả lỏng người giúp con nhé ạ!"
            cat = "HYPERTENSION_STAGE_2"
    elif max_severity == 2:
        note = "tâm thu đơn độc " if is_isolated_systolic else ""
        title = f"Tăng huyết áp {note}độ 1"
        advice = "Dạ thưa cụ, huyết áp hôm nay hơi cao nhẹ một chút so với mức chuẩn. Cụ nhớ duy trì chế độ ăn nhạt, tránh lo lắng và nghỉ ngơi nhé ạ!"
        cat = "HYPERTENSION_STAGE_1"
    elif max_severity == 1:
        if sys_cat == "MILD_LOW" or dia_cat == "MILD_LOW":
            title = "Huyết áp hơi thấp (Sát ngưỡng)"
            advice = "Dạ thưa cụ, huyết áp hôm nay hơi thấp nhẹ một chút so với mức chuẩn bình thường. Cụ ngồi nghỉ ngơi thả lỏng, uống một cốc nước ấm hoặc ăn nhẹ để huyết áp sớm ổn định lại nhé ạ!"
            cat = "HYPOTENSION_MILD"
        else:
            title = "Huyết áp bình thường cao"
            advice = "Dạ thưa cụ, chỉ số huyết áp của cụ ở mức an toàn ạ. Cụ duy trì tinh thần vui tươi, sinh hoạt điều độ như thế này nhé ạ!"
            cat = "HIGH_NORMAL"
    else:
        title = "Huyết áp rất đẹp (Chuẩn)"
        advice = "Dạ thưa cụ, chỉ số huyết áp hôm nay của cụ rất đẹp và lý tưởng ạ! Cụ tiếp tục duy trì sức khỏe thật tốt nhé ạ!"
        cat = "NORMAL"

    advice = advice + pulse_note

    return {
        "category": cat,
        "severity_level": max_severity,
        "title": title,
        "systolic": systolic,
        "diastolic": diastolic,
        "pulse": pulse,
        "pulse_status": pulse_status,
        "is_emergency": (max_severity == 4),
        "advice": advice
    }
