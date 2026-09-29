"""售罄场景 A/B 专用 harness：**把 login 移出循环**，隔离兑换拒绝路径本身。

为什么单独一个脚本：loadtest_redeem.jmx 每轮迭代都先登录，login 的 PBKDF2(10万次)
是 CPU 密集操作，单 worker 下会把 CPU 打满，redeem 的响应时间主要变成"排队等 CPU"，
DB 侧的优化（无锁预检）被完全掩盖。本脚本让每个线程**只登录一次**（Once Only Controller）
拿到 token 后循环只打 redeem，从而量到兑换路径的真实开销。

场景：造一个 stock 很小（默认 30）的专用礼品，20 线程猛打——瞬间售罄后，
剩余几乎全是"库存不足"的拒绝请求，正好压 before/after 差异最大的售罄拒绝路径。

用法：python portal/perf/setup_soldout.py   （生成 soldout_redeem.jmx + 复用 perf_users.csv）
环境变量：BASE / N / PW / POINTS_PER_USER / GIFT_STOCK / THREADS / DURATION / RAMP / ADMIN_USER / ADMIN_PW
仅标准库；对本地 Docker 实例做写操作（注册/发分/建礼品），属预期造数。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.getenv("BASE", "http://127.0.0.1:8000").rstrip("/")
N = int(os.getenv("N", "20"))
PW = os.getenv("PW", "Perf@12345")
POINTS_PER_USER = int(os.getenv("POINTS_PER_USER", "100000"))
GIFT_STOCK = int(os.getenv("GIFT_STOCK", "30"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PW = os.getenv("ADMIN_PW", "admin123")
THREADS = os.getenv("THREADS", str(N))
DURATION = os.getenv("DURATION", "30")
RAMP = os.getenv("RAMP", "5")

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "perf_users.csv"
JMX_PATH = HERE / "soldout_redeem.jmx"


def req(method: str, path: str, body=None, token: str = ""):
    url = BASE + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": raw[:200]}
        return e.code, payload


def main() -> None:
    st, r = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PW})
    if st != 200:
        raise SystemExit(f"[x] 管理员登录失败 {st}: {r}")
    atok = r["token"]

    accounts = []
    for i in range(1, N + 1):
        uname = f"perf{i:03d}"
        st, r = req("POST", "/api/auth/register",
                    {"username": uname, "password": PW, "name": f"压测号{i}", "department": "PERF"})
        if st == 200:
            emp = r["user"]["emp_id"]
        elif st == 409:
            st2, r2 = req("POST", "/api/auth/login", {"username": uname, "password": PW})
            if st2 != 200:
                raise SystemExit(f"[x] {uname} 冲突但登录失败 {st2}: {r2}")
            emp = r2["user"]["emp_id"]
        else:
            raise SystemExit(f"[x] 注册 {uname} 失败 {st}: {r}")
        req("POST", "/api/admin/points/adjust",
            {"emp_id": emp, "points": POINTS_PER_USER, "reason": "压测备分",
             "request_id": f"perfgrant{i:03d}pad"}, token=atok)
        accounts.append((uname, PW))
    print(f"[+] 已备 {len(accounts)} 个压测号（各 {POINTS_PER_USER} 分）")

    st, r = req("POST", "/api/admin/gifts",
                {"name": "售罄压测礼品(勿动)", "category": "压测", "points_cost": 1,
                 "stock": GIFT_STOCK, "icon": "🧪", "description": "售罄拒绝路径 A/B 专用"},
                token=atok)
    if st != 200:
        raise SystemExit(f"[x] 建礼品失败 {st}: {r}")
    gid = r["id"]
    print(f"[+] 售罄压测礼品 id={gid} stock={GIFT_STOCK} cost=1")

    CSV_PATH.write_text("".join(f"{u},{p}\n" for u, p in accounts), encoding="utf-8")
    jmx = (JMX_TEMPLATE.replace("__CSV__", str(CSV_PATH).replace("\\", "/"))
           .replace("__GIFT_ID__", str(gid))
           .replace("__THREADS__", THREADS)
           .replace("__DURATION__", DURATION)
           .replace("__RAMP__", RAMP))
    JMX_PATH.write_text(jmx, encoding="utf-8")
    print(f"[+] 写出 {JMX_PATH}（login 每线程仅一次，循环只打 redeem）")
    print(f"\n下一步：\n  & \"$env:JMETER_HOME\\bin\\jmeter.bat\" -n -t \"{JMX_PATH}\" "
          f"-l result.jtl -e -o report\n\n售罄礼品 id = {gid}（核对：应恰好 {GIFT_STOCK} 单成功、库存归 0、无超卖）")


# login 放进 Once Only Controller（每线程只跑一次拿 token），redeem 在其外循环。
JMX_TEMPLATE = r"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="soldout-redeem-only" enabled="true">
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <stringProp name="TestPlan.comments">售罄拒绝路径 A/B：login 仅一次，循环只打 redeem</stringProp>
    </TestPlan>
    <hashTree>
      <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="http-defaults" enabled="true">
        <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" enabled="true">
          <collectionProp name="Arguments.arguments"/>
        </elementProp>
        <stringProp name="HTTPSampler.domain">127.0.0.1</stringProp>
        <stringProp name="HTTPSampler.port">8000</stringProp>
        <stringProp name="HTTPSampler.protocol">http</stringProp>
        <stringProp name="HTTPSampler.contentEncoding">UTF-8</stringProp>
      </ConfigTestElement>
      <hashTree/>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="redeem-only" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <intProp name="LoopController.loops">-1</intProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">__THREADS__</stringProp>
        <stringProp name="ThreadGroup.ramp_time">__RAMP__</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">__DURATION__</stringProp>
        <stringProp name="ThreadGroup.delay"></stringProp>
        <boolProp name="ThreadGroup.same_user_on_next_iteration">true</boolProp>
      </ThreadGroup>
      <hashTree>
        <CSVDataSet guiclass="TestBeanGUI" testclass="CSVDataSet" testname="perf-users" enabled="true">
          <stringProp name="filename">__CSV__</stringProp>
          <stringProp name="fileEncoding">UTF-8</stringProp>
          <stringProp name="variableNames">usr,pwd</stringProp>
          <boolProp name="ignoreFirstLine">false</boolProp>
          <stringProp name="delimiter">,</stringProp>
          <boolProp name="quotedData">false</boolProp>
          <boolProp name="recycle">true</boolProp>
          <boolProp name="stopThread">false</boolProp>
          <stringProp name="shareMode">shareMode.all</stringProp>
        </CSVDataSet>
        <hashTree/>
        <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="headers" enabled="true">
          <collectionProp name="HeaderManager.headers">
            <elementProp name="" elementType="Header">
              <stringProp name="Header.name">Content-Type</stringProp>
              <stringProp name="Header.value">application/json</stringProp>
            </elementProp>
            <elementProp name="" elementType="Header">
              <stringProp name="Header.name">Authorization</stringProp>
              <stringProp name="Header.value">Bearer ${token}</stringProp>
            </elementProp>
          </collectionProp>
        </HeaderManager>
        <hashTree/>
        <OnceOnlyController guiclass="OnceOnlyControllerGui" testclass="OnceOnlyController" testname="login-once" enabled="true"/>
        <hashTree>
          <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="login" enabled="true">
            <boolProp name="HTTPSampler.postBodyRaw">true</boolProp>
            <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
              <collectionProp name="Arguments.arguments">
                <elementProp name="" elementType="HTTPArgument">
                  <boolProp name="HTTPArgument.always_encode">false</boolProp>
                  <stringProp name="Argument.value">{"username":"${usr}","password":"${pwd}"}</stringProp>
                  <stringProp name="Argument.metadata">=</stringProp>
                </elementProp>
              </collectionProp>
            </elementProp>
            <stringProp name="HTTPSampler.path">/api/auth/login</stringProp>
            <stringProp name="HTTPSampler.method">POST</stringProp>
            <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
            <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
          </HTTPSamplerProxy>
          <hashTree>
            <JSONPostProcessor guiclass="JSONPostProcessorGui" testclass="JSONPostProcessor" testname="extract-token" enabled="true">
              <stringProp name="JSONPostProcessor.referenceNames">token</stringProp>
              <stringProp name="JSONPostProcessor.jsonPathExprs">$.token</stringProp>
              <stringProp name="JSONPostProcessor.match_numbers">1</stringProp>
              <stringProp name="JSONPostProcessor.defaultValues">MISSING_TOKEN</stringProp>
            </JSONPostProcessor>
            <hashTree/>
          </hashTree>
        </hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="redeem" enabled="true">
          <boolProp name="HTTPSampler.postBodyRaw">true</boolProp>
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
            <collectionProp name="Arguments.arguments">
              <elementProp name="" elementType="HTTPArgument">
                <boolProp name="HTTPArgument.always_encode">false</boolProp>
                <stringProp name="Argument.value">{"request_id":"so${__UUID}"}</stringProp>
                <stringProp name="Argument.metadata">=</stringProp>
              </elementProp>
            </collectionProp>
          </elementProp>
          <stringProp name="HTTPSampler.path">/api/user/gifts/__GIFT_ID__/redeem</stringProp>
          <stringProp name="HTTPSampler.method">POST</stringProp>
          <boolProp name="HTTPSampler.follow_redirects">true</boolProp>
          <boolProp name="HTTPSampler.use_keepalive">true</boolProp>
        </HTTPSamplerProxy>
        <hashTree>
          <ResponseAssertion guiclass="AssertionGui" testclass="ResponseAssertion" testname="assert-200" enabled="true">
            <collectionProp name="Asserion.test_strings">
              <stringProp name="49586">200</stringProp>
            </collectionProp>
            <stringProp name="Assertion.test_field">Assertion.response_code</stringProp>
            <boolProp name="Assertion.assume_success">false</boolProp>
            <intProp name="Assertion.test_type">8</intProp>
          </ResponseAssertion>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


if __name__ == "__main__":
    main()
