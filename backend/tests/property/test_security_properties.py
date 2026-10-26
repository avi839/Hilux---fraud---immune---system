"""API_Server bind-address classification property (Req 27.1, 27.2)."""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.api.security import is_loopback

pytestmark = pytest.mark.property

LOOP4 = ipaddress.ip_network("127.0.0.0/8")
LOOP6 = ipaddress.ip_address("::1")

# Mix uniform addresses with the interesting neighbourhoods (127/8 edges, ::1, 0.0.0.0, ::).
addresses = st.one_of(
    st.ip_addresses(v=4),
    st.ip_addresses(v=6),
    st.ip_addresses(network="127.0.0.0/8"),
    st.ip_addresses(network="126.255.255.0/24"),
    st.ip_addresses(network="128.0.0.0/24"),
    st.ip_addresses(network="::/120"),
    st.ip_addresses(network="::ffff:127.0.0.0/120"),
    st.sampled_from([ipaddress.ip_address(a) for a in ("0.0.0.0", "::", "127.0.0.1", "::1")]),
)


# Feature: helix-fraud-immune-system, non-loopback bind detection (task 11.2)
@settings(max_examples=500, deadline=None)
@given(ip=addresses, bracket=st.booleans())
def test_is_loopback_matches_ipaddress(ip, bracket):
    """For any IPv4/IPv6 address, ``is_loopback`` (on its text form, and on the bracketed
    form for IPv6) equals ``ipaddress.ip_address(...).is_loopback``, which on this Python is
    exactly membership of 127.0.0.0/8 or equality with ::1 (Req 27.2's definition).

    **Validates: Requirements 27.1, 27.2**
    """
    text = str(ip)
    if bracket and ip.version == 6:
        text = f"[{text}]"
    expected = ipaddress.ip_address(str(ip)).is_loopback
    assert is_loopback(text) is expected
    assert expected == ((ip.version == 4 and ip in LOOP4) or ip == LOOP6)
