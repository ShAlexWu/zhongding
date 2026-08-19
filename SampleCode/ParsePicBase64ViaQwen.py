import os
import base64
from dashscope import MultiModalConversation

# 1. 配置 API Key（建议从环境变量读取，或直接替换为 "sk-xxx"）
api_key = os.getenv("DASHSCOPE_API_KEY")

# 2. 读取本地图片并转换为 Base64 字符串
local_image_path = "xxx/eagle.png"  # 请替换为你本地图像的绝对路径
with open(local_image_path, "rb") as f:
    image_data = base64.b64encode(f.read()).decode("utf-8")

# 3. 构造消息体（DashScope 原生格式支持直接传入 base64 字符串或文件路径）
messages = [
    {
        'role': 'user',
        'content': [
            {'image': image_data},       # 传入 Base64 编码的图片数据
            {'text': '仔细观察图片中的内容，如果全是文字而没有机械零件设计的图形，返回 markdown 格式，保留文本和表格的格式，否则返回数值 0。不要输出推理和思考过程，直接输出结果。'}  # 你的提问
        ]
    }
]

# 4. 调用 qwen3.7-plus 模型进行多模态理解
response = MultiModalConversation.call(
    api_key=api_key,
    model='qwen3.7-plus',
    messages=messages
)

# 5. 处理并打印返回结果
if response.status_code == 200:
    print(response.output.choices[0].message.content[0]["text"])
else:
    print(f"调用失败: {response.code}, {response.message}")