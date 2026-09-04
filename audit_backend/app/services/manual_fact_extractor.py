"""One-call AI extraction for all non-identity manual checks."""

from __future__ import annotations

import json

from app.services.doc_parser import ManualDoc
from app.services.vlm_client import VLMClient


class ManualFactExtractor:
    def __init__(self, model: VLMClient) -> None:
        self.model = model

    async def extract(self, manual: ManualDoc, inputs: dict) -> dict:
        tech_req = str(inputs.get("tech_req") or "")
        new_material = str(inputs.get("new_material") or "")
        revision_reason = inputs.get("revision_reason") or {}
        prompt = f"""你是集装箱技术说明书信息抽取器。只提取事实，不根据常识补全，不联网。
把“客户技术要求”和“新材料/新技术/工艺改进”逐条拆成原子检查点，不得漏掉任何原文片段。
在说明书中寻找逐字证据。同一对象和属性存在但值与客户要求不同时，status=mismatch，actual_value 填说明书实际值，evidence 返回对应原文；只有同一对象和属性完全不存在时才 status=missing，语义不唯一时 status=uncertain。
contacts 只允许提取页脚/footer 中明确出现的姓名、邮箱或电话；正文数字、标准编号和日期不得作为联系人。
严格返回以下 JSON，不要增加字段：
{{
  "change_checks": [{{"source_text":"输入中的完整原文片段", "requirement":"", "status":"found|mismatch|missing|uncertain", "actual_value":"", "evidence":""}}],
  "requirement_facts": [{{"source_text":"输入中的完整原文片段", "object":"审核对象原文", "attribute":"thickness|color|material|process|quantity|guarantee|trademark|contact|other", "expected_value":"原值或数值", "unit":"原单位", "target_hint":"manual|drawing|trademark|unknown"}}],
  "revision_items": [{{"text":"", "evidence":""}}],
  "contacts": [{{"name":"", "email":"", "phone":"", "evidence":""}}],
  "paint": {{
    "exterior": {{"color":"", "ral":"", "evidence":""}},
    "interior": {{"color":"", "ral":"", "evidence":""}}
  }},
  "trademark": {{"material":"", "evidence":""}},
  "guarantees": [{{"subject":"", "duration":null, "unit":"年|月", "conditions":"", "evidence":""}}],
  "requirements": {{
    "contacts": [{{"name":"", "email":"", "phone":""}}],
    "paint": {{"exterior": {{"color":"", "ral":""}}, "interior": {{"color":"", "ral":""}}}},
    "trademark": {{"material":""}},
    "guarantees": [{{"subject":"", "duration":null, "unit":"年|月", "conditions":""}}]
  }}
}}

客户技术要求：
{tech_req}

新材料/新技术/工艺改进：
{new_material}

修改原因：
{json.dumps(revision_reason, ensure_ascii=False)}

说明书全文：
{manual.full_text[:100_000]}
"""
        facts = await self.model.chat_json(prompt)
        return facts if isinstance(facts, dict) else {}
