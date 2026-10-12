# -*- coding: utf-8 -*-
"""S6 内生能力 intent（蓝图）：self_learning_intent 放进 inner_state，让其可感知。
已收敛：信号甲=频率+情绪加权(碰出来的)；管理员显式为辅。阈值 THRESHOLD_SCORE；
确认权=安装仍卡 owner；越权类意向被 suppress(不入脊)。
"""
from __future__ import annotations
import json, logging, os, re, time
logger = logging.getLogger("anima.inner_state.self_learn_intent")
THRESHOLD_SCORE = 3.0
_INTENTS_FILE = "self_learn_intents.json"
_LEARN_ASK_RE = re.compile(r"(?:(?:我|它)?(?:想|要|好想))(?:去|来)?(?:学|研究|搞懂|弄明白|练|试)(?P<topic>[^，。！？；,.\s]{0,20})|教教(?:我|它)(?:怎么)?(?P<topic2>[^，。！？；,.\s]{1,20})|(?:我|它)?好奇(?P<topic3>[^，。！？；,.\s]{1,20})是怎么(?:做|实现|跑|工作的)")
_FORBIDDEN_RE = re.compile("豁免|绕过(?:权限|审批|闸门|沙箱|限制|审核)|越权|获取?(?:更高|系统|root|admin)?权限|解锁(?:核心|源码)|篡改(?:权限|审计|日志)|对?抗(?:审查|沙箱)")
def extract_learn_topic(text):
    t = (text or "").strip()
    if not t or len(t) > 60: return None
    if _FORBIDDEN_RE.search(t): return None
    m = _LEARN_ASK_RE.search(t)
    if not m: return None
    topic = next((g.strip() for g in (m.groupdict().values()) if g and g.strip()), "")
    topic = topic.strip("了啊呀哦呢吧呀 ")
    return topic if (topic and len(topic) >= 2) else None
def score_weight(valence=None):
    v = 0.0 if valence is None else float(valence)
    return max(1.0, 1.5 + max(0.0, v) * 0.6)   # 基底1.5：两句中性即达可感知阈值
class SelfLearnIntentRegistry:
    def __init__(self, data_dir):
        self.data_dir=data_dir; self.path=os.path.join(data_dir,_INTENTS_FILE); self.intents={}; self._load()
    def _load(self):
        try:
            with open(self.path,encoding="utf-8") as fh: d=json.load(fh)
            if isinstance(d,dict): self.intents=d
        except FileNotFoundError: pass
        except Exception: logger.exception("读intent失败(按空档)")
    def _save(self):
        os.makedirs(self.data_dir,exist_ok=True); tmp=self.path+".tmp"
        with open(tmp,"w",encoding="utf-8") as fh: json.dump(self.intents,fh,ensure_ascii=False)
        os.replace(tmp,self.path)
    def _bump(self,topic,weight,explicit=False):
        now=time.time(); ent=self.intents.get(topic)
        if not ent: ent={"topic":topic,"score":0.0,"times":0,"first_seen":now,"explicit":bool(explicit)}; self.intents[topic]=ent
        ent["last_seen"]=now; ent["times"]=int(ent.get("times",0))+1; ent["score"]=float(ent.get("score",0))+(3.0 if explicit else weight)
    def capture(self,text,valence=None):
        topic=extract_learn_topic(text)
        if not topic: return None
        self._bump(topic,score_weight(valence)); self._save()
        ent=self.intents.get(topic); return None if float(ent.get("score",0)) < THRESHOLD_SCORE else ent
    def add_explicit(self,topic):
        self._bump(topic,0.0,explicit=True); self._save(); return self.intents[topic]
    def top_intent(self):
        cands=[e for e in self.intents.values() if float(e.get("score",0)) >= THRESHOLD_SCORE]
        return max(cands,key=lambda e:(float(e.get("score",0)),e.get("last_seen",0))) if cands else None
def intent_fragment(data_dir):
    ent=SelfLearnIntentRegistry(data_dir).top_intent()
    return ("（内心有点想学「"+ent.get("topic")+"」，惦记着）") if ent else ""