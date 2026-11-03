from cryptography import x509

from lm_agent.web.tls import ensure_certificate


def san_of(path):
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return set(san.get_values_for_type(x509.DNSName)), {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def test_certificate_issued_and_reused(tmp_path):
    cert, key = ensure_certificate(tmp_path, ["localhost", "mac.local"], ["192.168.1.47"])
    names, ips = san_of(cert)
    assert names == {"localhost", "mac.local"} and ips == {"127.0.0.1", "192.168.1.47"}
    assert oct(key.stat().st_mode)[-3:] == "600"  # ключ читает только владелец
    before = cert.read_bytes()
    ensure_certificate(tmp_path, ["localhost", "mac.local"], ["192.168.1.47"])
    assert cert.read_bytes() == before  # адреса те же — сертификат не перевыпущен


def test_certificate_renewed_when_address_changes(tmp_path):
    cert, _ = ensure_certificate(tmp_path, ["localhost"], ["192.168.1.47"])
    before = cert.read_bytes()
    ensure_certificate(tmp_path, ["localhost"], ["192.168.1.80"])  # роутер выдал новый адрес
    assert cert.read_bytes() != before and "192.168.1.80" in san_of(cert)[1]
