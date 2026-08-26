# -*- coding: utf-8 -*-
"""M2.1 回归：WS action 回执分类——NapCat 真失败绝不当成功（静默丢消息修复）。
"""
from anima.channels.onebot11 import _classify_ws_send_result


def test_message_id_confirmed():
    assert _classify_ws_send_result({"data": {"message_id": 9}}) == 9
    assert _classify_ws_send_result({"message_id": 7, "status": "ok"}) == 7

def test_ok_without_id_returns_success_placeholder():
    assert _classify_ws_send_result({"status": "ok"}) == 1
    assert _classify_ws_send_result({"retcode": 0}) == 1

def test_real_failure_returns_none_not_success():
    assert _classify_ws_send_result({"status": "failed", "data": None, "retcode": 140008}) is None
    assert _classify_ws_send_result({"data": None, "status": "failed"}) is None
    assert _classify_ws_send_result({"retcode": 100, "status": "failed"}) is None

def test_non_dict_response_returns_none():
    assert _classify_ws_send_result(None) is None
    assert _classify_ws_send_result("not-a-dict") is None