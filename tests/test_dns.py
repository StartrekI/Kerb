"""The MX lookup behind site_contact's "can this address receive mail?".

Offline: the packets are built by hand, and the network calls are replaced.

    python3 tests/test_dns.py
"""

import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kerb import dns                                          # noqa: E402


def answer(qid, flags, question, records):
    """A DNS response: the question echoed, then `records` MX answers."""
    head = struct.pack(">HHHHHH", qid, flags, 1, len(records), 0, 0)
    body = b""
    for pref, host in records:
        rdata = struct.pack(">H", pref) + b"".join(
            bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
        body += struct.pack(">HHHIH", 0xC00C, dns.MX, 1, 300, len(rdata)) + rdata
    return head + question + body


def test_answers_are_counted_and_untrusted_ones_refused():
    q = dns.build_query("example.co.uk", qid=0x1234)
    assert q[12:] == b"\x07example\x02co\x02uk\x00\x00\x0f\x00\x01"
    question = q[12:]
    ok = answer(0x1234, 0x8180, question, [(10, "mx1.example.co.uk"), (20, "mx2.example.co.uk")])
    assert dns.count_answers(ok, 0x1234) == 2
    assert dns.count_answers(answer(0x1234, 0x8183, question, []), 0x1234) == 0, "NXDOMAIN"
    assert dns.count_answers(ok, 0x9999) is None, "someone else's answer"
    assert dns.count_answers(answer(0x1234, 0x8182, question, []), 0x1234) is None, "SERVFAIL"
    assert dns.count_answers(ok[:20], 0x1234) is None, "truncated"
    assert dns.count_answers(b"", 0x1234) is None
    print("  answers counted          ok")


def test_has_mail_uses_the_address_record_when_there_is_no_mx():
    real_mx, real_gai = dns.mx_count, socket.getaddrinfo
    try:
        dns._CACHE.clear()
        dns.mx_count = lambda d, timeout=2.0: {"mx.example": 2, "bare.example": 0,
                                               "gone.example": 0}.get(d)
        socket.getaddrinfo = lambda d, port: ([("A",)] if d == "bare.example"
                                              else (_ for _ in ()).throw(socket.gaierror("no")))
        assert dns.has_mail("MX.example") is True
        assert dns.has_mail("bare.example") is True, "no MX but an address: RFC 5321"
        assert dns.has_mail("gone.example") is False
        assert dns.has_mail("silent.example") is None, "no answer is unknown, not no"
        assert dns.has_mail("") is None and dns.has_mail("localhost") is None
        dns.mx_count = lambda d, timeout=2.0: 1 / 0          # cached: never asked again
        assert dns.has_mail("mx.example") is True
    finally:
        dns.mx_count, socket.getaddrinfo = real_mx, real_gai
        dns._CACHE.clear()
    print("  has_mail                 ok")


def test_a_network_that_never_answers_stops_being_asked():
    """With DNS blocked, every email would otherwise cost its timeout."""
    real = dns.resolvers
    try:
        dns._SILENT["run"] = 0
        asked = []
        dns.resolvers = lambda path="/etc/resolv.conf": asked.append(1) or []
        for _ in range(dns.GIVE_UP_AFTER + 3):
            assert dns.mx_count("x.example") is None
        assert len(asked) == dns.GIVE_UP_AFTER, asked
    finally:
        dns.resolvers = real
        dns._SILENT["run"] = 0
    print("  gives up on silence      ok")


if __name__ == "__main__":
    print("dns -- can an address receive mail?\n")
    for fn in (test_answers_are_counted_and_untrusted_ones_refused,
               test_has_mail_uses_the_address_record_when_there_is_no_mx,
               test_a_network_that_never_answers_stops_being_asked):
        fn()
    print("\nall dns checks passed")
