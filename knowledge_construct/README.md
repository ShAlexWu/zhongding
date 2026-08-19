# 图纸知识库构建（knowledge_construct）

对 `图纸/` 目录下每张已解析图纸进行向量化，并写入 MatrixOne（MO）向量数据库，
为后续"以图/以文检索历史图纸"提供基础。

## 完成的任务（对应 Prompt.md「图片知识库」）

1. 用 **qwen3-vl-embedding** 对每张图纸 `images/` 下各视图图片向量化，写入表 `view_pic_vec`
   （字段：`diagram_name`、`pic_name`、`vec_value`）。
2. 创建表 `view_text_vec`（字段：`diagram_name`、`chunk_name`、`vec_value`）。
3. 对 md 文件按「最高一级标题」分段，分段正文不含图片链接文本。
4. 用 **text-embedding-v4** 对分段向量化，写入 `view_text_vec`。
5. MO 建表 / 写入 / 连接方式参考根目录《MO 创建向量数据表和查询.md》。

## 文件说明

| 文件 | 作用 |
|---|---|
| `config.py` | 数据库连接、模型名、向量维度、目录等集中配置 |
| `mo_db.py` | MO 连接、建表、向量写入 |
| `embedding_client.py` | 图片 / 文本向量化封装（DashScope） |
| `markdown_chunker.py` | md 分段（按最高一级标题，剔除图片链接） |
| `build_knowledge_base.py` | 主程序，串联以上步骤 |

## 分段规则说明

「最高一级标题」实现为：**除唯一的文档标题（一级 `#`）外，第一个出现次数大于 1 的标题层级**。
- `20C114257_extracted.md` 按 `##` 切分，得到「主加工视图」「A-A 剖面视图」等（与示例一致）。
- 仅含一张图片的小节（如「整页 PNG」）在剔除图片链接后正文为空，自动跳过。

## 运行

```powershell
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 DashScope 密钥（必需）
$env:DASHSCOPE_API_KEY = "sk-xxxx"

# 3.（可选）覆盖 MO 连接，默认使用 MO 指引中的示例账号
$env:MO_HOST = "..."; $env:MO_USER = "..."; $env:MO_PASSWORD = "..."; $env:MO_DB = "zhongding"

# 4. 构建全部图纸
python build_knowledge_base.py

# 仅构建指定图纸（按文件夹名）
python build_knowledge_base.py 20C114220
```

脚本可重复运行：每次会先删除该图纸在两张表中的旧记录，再重新写入，避免重复。

## 向量维度

`text-embedding-v4` 与 `qwen3-vl-embedding` 统一取 **1024** 维（两者均支持），
与 MO 指引示例中的 `vecf64(1024)` 一致，便于两表结构统一。可在 `config.py` 的
`VECTOR_DIM` 调整。
