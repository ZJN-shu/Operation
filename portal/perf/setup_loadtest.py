"""兑换写路径压测的**数据准备 + JMeter 计划生成器**（仅标准库，走真实 HTTP API）。

它做四件事，全部通过后端接口，不猜数据库口令：
  1) 注册 N 个压测普通用户（perf001..perfNNN），拿到各自 emp_id；
  2) 用超管 admin 给每个号人工发放 POINTS_PER_USER 积分（够跑满一分钟不被积分卡住）；
  3) 建一个专用高库存礼品（stock=GIFT_STOCK、points_cost=1），避免污染演示礼品；
  4) 写出 perf_users.csv 与 loadtest_redeem.jmx，并把礼品 id 直接嵌进 jmx。

用法（项目根 portal 之外也可，脚本按 BASE 打 HTTP）：
    python -m portal.perf.setup_loadtest
环境变量可覆盖：BASE / N / PW / POINTS_PER_USER / GIFT_STOCK / ADMIN_USER / ADMIN_PW
只依赖标准库；对本地 Docker 实例（默认 127.0.0.1:8000）执行写操作，属预期的造数。
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
GIFT_STOCK = int(os.getenv("GIFT_STOCK", "1000000"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PW = os.getenv("ADMIN_PW", "admin123")
THREADS = os.getenv("THREADS", str(N))
DURATION = os.getenv("DURATION", "60")
RAMP = os.getenv("RAMP", "5")

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "perf_users.csv"
JMX_PATH = HERE / "loadtest_redeem.jmx"


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
    # 0) 超管登录
    st, r = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PW})
    if st != 200:
        raise SystemExit(f"[x] 管理员登录失败 {st}: {r}（确认 DEMO 种子已灌、admin/{ADMIN_PW} 可用）")
    atok = r["token"]
    print(f"[+] 超管登录成功 emp={r['user']['emp_id']}")

    # 1+2) 注册压测号并发积分
    accounts = []
    for i in range(1, N + 1):
        uname = f"perf{i:03d}"
        st, r = req("POST", "/api/auth/register",
                    {"username": uname, "password": PW, "name": f"压测号{i}", "department": "PERF"})
        if st == 200:
            emp = r["user"]["emp_id"]
        elif st == 409:  # 已存在（重复跑）：登录拿 emp
            st2, r2 = req("POST", "/api/auth/login", {"username": uname, "password": PW})
            if st2 != 200:
                raise SystemExit(f"[x] {uname} 注册冲突但登录失败 {st2}: {r2}")
            emp = r2["user"]["emp_id"]
        else:
            raise SystemExit(f"[x] 注册 {uname} 失败 {st}: {r}")
        # 发积分（幂等键固定，重复跑不会二次到账）
        st3, r3 = req("POST", "/api/admin/points/adjust",
                      {"emp_id": emp, "points": POINTS_PER_USER, "reason": "压测备分",
                       "request_id": f"perfgrant{i:03d}pad"}, token=atok)
        if st3 not in (200,):
            # 409=同 request_id 重放（已发过），属正常幂等；其它才算错
            if not (st3 == 409 or "已存在" in str(r3.get("detail", ""))):
                raise SystemExit(f"[x] 给 {emp} 发积分失败 {st3}: {r3}")
        accounts.append((uname, PW))
    print(f"[+] 已备 {len(accounts)} 个压测号，每个 {POINTS_PER_USER} 分")

    # 3) 专用压测礼品（cost=1，高库存；每次跑都新建一个，互不干扰）
    st, r = req("POST", "/api/admin/gifts",
                {"name": "压测礼品(勿动)", "category": "压测", "points_cost": 1,
                 "stock": GIFT_STOCK, "icon": "🔥", "description": "JMeter 兑换写路径压测专用"},
                token=atok)
    if st != 200:
        raise SystemExit(f"[x] 建礼品失败 {st}: {r}")
    gid = r["id"]
    print(f"[+] 压测礼品 id={gid} stock={GIFT_STOCK} cost=1")

    # 4) CSV + jmx
    CSV_PATH.write_text("".join(f"{u},{p}\n" for u, p in accounts), encoding="utf-8")
    jmx = JMX_TEMPLATE.replace("__CSV__", str(CSV_PATH).replace("\\", "/")) \
                      .replace("__GIFT_ID__", str(gid)) \
                      .replace("__THREADS__", THREADS) \
                      .replace("__DURATION__", DURATION) \
                      .replace("__RAMP__", RAMP)
    JMX_PATH.write_text(jmx, encoding="utf-8")
    print(f"[+] 写出 {CSV_PATH}")
    print(f"[+] 写出 {JMX_PATH}")
    print("\n下一步（无 GUI 跑压测，20 线程 × 60s）：\n"
          f'  & "$env:JMETER_HOME\\bin\\jmeter.bat" -n -t "{JMX_PATH}" '
          f'-l "{HERE / "result.jtl"}" -e -o "{HERE / "report"}" '
          f'-Jjmeter.save.saveservice.response_data=true\n'
          f"\n压测礼品 id = {gid}（测完据此核对不超卖 / 账实一致）")


JMX_TEMPLATE = r"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="redeem-load" enabled="true">
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <stringProp name="TestPlan.comments">兑换写路径压测：20线程抢同一高库存礼品</stringProp>
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
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="redeem" enabled="true">
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
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="redeem" enabled="true">
          <boolProp name="HTTPSampler.postBodyRaw">true</boolProp>
          <elementProp name="HTTPsampler.Arguments" elementType="Arguments">
            <collectionProp name="Arguments.arguments">
              <elementProp name="" elementType="HTTPArgument">
                <boolProp name="HTTPArgument.always_encode">false</boolProp>
                <stringProp name="Argument.value">{"request_id":"perf${__UUID}"}</stringProp>
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
