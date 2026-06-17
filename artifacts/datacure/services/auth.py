import base64
import re


def encode_phone(phone: str) -> str:
    return base64.b64encode(phone.encode()).decode()


def decode_phone(encoded: str) -> str:
    try:
        return base64.b64decode(encoded.encode()).decode()
    except Exception:
        return encoded


def detect_operator(phone: str) -> str:
    if re.match(r"^(98|99|97|70|71|72|73|74|75|76|77|78|79|80|81|82|83|84|85|86|87|88|89)", phone):
        airtel = re.match(r"^(98|99|97)", phone)
        jio = re.match(r"^(70|71|72|73|74|75|76|77|78|79|80|81|82|83|84|85|86|87|88|89)", phone)
        if airtel:
            return "Airtel"
        if jio:
            return "Jio"
    if re.match(r"^(90|91|92|93|94|95|96)", phone):
        return "Airtel"
    if re.match(r"^(70|71|72|73|74|75|76|77|78|79)", phone):
        return "Jio"
    if re.match(r"^(60|61|62|63|64|65|66|67|68|69)", phone):
        return "Vi"
    if re.match(r"^(94|95|96)", phone):
        return "BSNL"
    return "Unknown"


def validate_name(name: str):
    if not name:
        return "Name is required."
    if len(name) < 3:
        return "Name must be at least 3 characters."
    return None


def validate_user_id(uid: str):
    if not uid:
        return "User ID is required."
    if len(uid) < 3:
        return "User ID must be at least 3 characters."
    if len(uid) > 30:
        return "User ID must be 30 characters or fewer."
    if not re.match(r'^[A-Za-z0-9_.\-]+$', uid):
        return "User ID may only contain letters, numbers, _ . and -"
    return None


def validate_phone(phone: str):
    if not phone:
        return "Phone number is required."
    if not phone.isdigit() or len(phone) != 10:
        return "Phone must be exactly 10 digits."
    return None


def validate_password(password: str):
    if not password:
        return "Password is required."
    if len(password) < 6:
        return "Password must be at least 6 characters."
    return None
