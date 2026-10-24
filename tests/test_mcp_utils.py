"""mcp_utils.clamp_limit 单测：limit 归一化的全部边界。"""
import mcp_utils
from mcp_utils import clamp_limit


def test_none_falls_back_to_default():
    assert clamp_limit(None, 20) == 20
    assert clamp_limit(None, 5) == 5


def test_zero_and_negative_fall_back_to_default():
    assert clamp_limit(0, 20) == 20
    assert clamp_limit(-5, 10) == 10


def test_non_numeric_falls_back_to_default():
    assert clamp_limit("abc", 20) == 20
    assert clamp_limit([], 7) == 7


def test_numeric_string_is_coerced():
    assert clamp_limit("30", 20) == 30


def test_above_max_is_truncated_not_error():
    assert clamp_limit(1000, 20) == 200
    assert clamp_limit(10 ** 9, 5) == 200


def test_in_range_values_pass_through():
    assert clamp_limit(1, 20) == 1
    assert clamp_limit(20, 5) == 20
    assert clamp_limit(200, 20) == 200


def test_custom_bounds():
    assert clamp_limit(500, 20, lo=1, hi=50) == 50
    assert clamp_limit(0, 20, lo=1, hi=50) == 20


def test_limit_max_constant():
    assert mcp_utils.LIMIT_MAX == 200
