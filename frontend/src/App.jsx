import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

function pct(x) {
  return (x * 100).toFixed(2) + '%'
}

function MatchingPage() {
  const [file, setFile] = useState(null)
  const [imageWeight, setImageWeight] = useState(50) // 0-100，文本权重=100-该值
  const [running, setRunning] = useState(false)
  const [logs, setLogs] = useState([]) // 通用/串行思考过程（无 worker 的 progress）
  const [mode, setMode] = useState('')
  const [workerLogs, setWorkerLogs] = useState([]) // 每个线程窗口一组行
  const [stageTimings, setStageTimings] = useState(null) // 各阶段耗时（匹配完成后才有）
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [pdfDiagrams, setPdfDiagrams] = useState(() => new Set()) // 有原始 PDF 的图纸名集合
  const logRef = useRef(null)

  useEffect(() => {
    fetch('/api/pdf_diagrams')
      .then((r) => r.json())
      .then((d) => setPdfDiagrams(new Set(d.diagrams || [])))
      .catch(() => {})
  }, [])

  function appendLog(line) {
    setLogs((prev) => {
      const next = [...prev, line]
      return next
    })
    requestAnimationFrame(() => {
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
    })
  }

  async function handleUpload() {
    if (!file) {
      setError('请先选择一张图片')
      return
    }
    setRunning(true)
    setError('')
    setLogs([])
    setWorkerLogs([])
    setStageTimings(null)
    setResult(null)
    setMode('')

    const fd = new FormData()
    fd.append('file', file)
    fd.append('image_weight', String(imageWeight / 100))
    fd.append('text_weight', String((100 - imageWeight) / 100))

    try {
      const resp = await fetch('/api/upload', { method: 'POST', body: fd })
      if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status)

      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buf = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // 按 SSE 消息分隔（空行）
        const parts = buf.split('\n\n')
        buf = parts.pop() // 余下不完整的一段
        for (const part of parts) {
          handleSseChunk(part)
        }
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setRunning(false)
    }
  }

  function handleSseChunk(chunk) {
    // chunk 形如 "event: xxx\ndata: {...}"
    let dataLine = ''
    for (const line of chunk.split('\n')) {
      if (line.startsWith('data:')) dataLine += line.slice(5).trim()
    }
    if (!dataLine) return
    let evt
    try {
      evt = JSON.parse(dataLine)
    } catch {
      return
    }
    if (evt.type === 'progress') {
      if (typeof evt.worker === 'number') appendWorkerLog(evt.worker, evt.msg)
      else appendLog(evt.msg)
    } else if (evt.type === 'match_start') {
      // 进入并行阶段：按线程数创建对应数量的窗口
      setWorkerLogs(Array.from({ length: evt.workers }, () => []))
      appendLog(`开始并行比对：共 ${evt.total} 张图纸，${evt.workers} 个比对通道…`)
    } else if (evt.type === 'mode') {
      // 模式只在顶部统一展示一次，不写进任何窗口/日志
      setMode(evt.mode)
    } else if (evt.type === 'result') {
      setResult(evt)
      setMode(evt.mode)
      setStageTimings(evt.stage_timings || null)
    } else if (evt.type === 'error') {
      setError(evt.msg)
      appendLog('【错误】' + evt.msg)
    }
  }

  function appendWorkerLog(idx, line) {
    setWorkerLogs((prev) => {
      const next = prev.slice()
      if (!next[idx]) next[idx] = []
      next[idx] = [...next[idx], line]
      return next
    })
  }

  return (
    <div className="match-page">
      <p className="hint">
        上传局部零件的图片或者技术参数图片（png/jpg/bmp），系统将推荐高
        <span
          className="tip tip-text"
          data-tip="系统会从图形相似性和文本内容接近度两个维度来综合计算相似度"
        >
          相似度
        </span>
        的图纸。
      </p>

      <div className="controls">
        <input
          type="file"
          accept=".png,.jpg,.jpeg,.bmp,image/*"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
          disabled={running}
        />
        <div className="weight-box">
          <label>
            图片权重 {imageWeight}% ／ 文本权重 {100 - imageWeight}%
            <span
              className="help-icon tip"
              data-tip="设置图形相似性和文本内容接近度在判断相似度时的权重"
            >
              ?
            </span>
          </label>
          <input
            type="range"
            min="0"
            max="100"
            step="5"
            value={imageWeight}
            onChange={(e) => setImageWeight(Number(e.target.value))}
            disabled={running}
          />
        </div>
        <button className="search-btn" onClick={handleUpload} disabled={running}>
          {running ? '处理中…' : '上传并匹配'}
        </button>
      </div>

      {error && <div className="error">出错：{error}</div>}

      {(logs.length > 0 || running) && (
        <div className="thinking-live">
          <h2>AI 思考过程</h2>
          {mode && (
            <div className="mode-banner">
              【匹配模式】{mode === 'graphic' ? '图形和文本综合匹配' : '文本匹配'}
            </div>
          )}
          <pre ref={logRef} className="log-box">
            {logs.join('\n')}
            {running ? '\n…' : ''}
          </pre>

          {stageTimings && <StageTimings timings={stageTimings} />}

          {workerLogs.length > 0 && (
            <div className="worker-grid">
              {workerLogs.map((lines, i) => (
                <WorkerWindow key={i} index={i} lines={lines} running={running} />
              ))}
            </div>
          )}
        </div>
      )}

      {result && result.mode === 'graphic' && <GraphicResult result={result} pdfDiagrams={pdfDiagrams} />}
      {result && result.mode === 'text' && <TextResult result={result} pdfDiagrams={pdfDiagrams} />}
    </div>
  )
}

// 页面顶部品牌区：logo + 标题 + 副标题；登录后额外带一个退出登录按钮
function AppBrand({ onLogout }) {
  return (
    <div className="app-brand">
      <div className="app-brand-left">
        <img className="app-logo" src="/logo.svg" alt="MatrixOrigin" />
        <div className="app-brand-titles">
          <h1 className="app-title">图纸智能应用智能体</h1>
          <div className="app-subtitle">由 MatrixOrigin 矩阵起源 AI 数智平台生成</div>
        </div>
      </div>
      {onLogout && (
        <button className="logout-btn" onClick={onLogout}>
          退出登录
        </button>
      )}
    </div>
  )
}

function LoginPage({ onLoggedIn }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!username || !password) {
      setError('请输入用户名和密码')
      return
    }
    setLoading(true)
    setError('')
    try {
      const fd = new FormData()
      fd.append('username', username)
      fd.append('password', password)
      const resp = await fetch('/api/login', { method: 'POST', body: fd })
      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}))
        throw new Error(j.detail || `登录失败（HTTP ${resp.status}）`)
      }
      onLoggedIn()
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <AppBrand />
        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            用户名
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoFocus
              disabled={loading}
            />
          </label>
          <label>
            密码
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={loading}
            />
          </label>
          {error && <div className="error">{error}</div>}
          <button type="submit" className="search-btn" disabled={loading}>
            {loading ? '登录中…' : '登录'}
          </button>
        </form>
      </div>
    </div>
  )
}

export default function App() {
  // 'loading'：正在查会话状态；'in'：已登录；'out'：未登录，展示登录页
  const [authState, setAuthState] = useState('loading')
  const [activeTab, setActiveTab] = useState('match')

  useEffect(() => {
    fetch('/api/session')
      .then((r) => r.json())
      .then((d) => setAuthState(d.authenticated ? 'in' : 'out'))
      .catch(() => setAuthState('out'))
  }, [])

  async function handleLogout() {
    try {
      await fetch('/api/logout', { method: 'POST' })
    } catch {
      // 忽略：即便请求失败，前端也直接切回登录页
    }
    setAuthState('out')
  }

  if (authState === 'loading') {
    return <div className="page app-loading">加载中…</div>
  }
  if (authState === 'out') {
    return <LoginPage onLoggedIn={() => setAuthState('in')} />
  }

  return (
    <div className="page">
      <AppBrand onLogout={handleLogout} />
      <div className="tabs">
        <button
          className={`tab ${activeTab === 'match' ? 'active' : ''}`}
          onClick={() => setActiveTab('match')}
        >
          图纸检索
        </button>
        <button
          className={`tab ${activeTab === 'doc' ? 'active' : ''}`}
          onClick={() => setActiveTab('doc')}
        >
          文档生成 - 生产检验指导书
        </button>
        <button
          className={`tab ${activeTab === 'guide' ? 'active' : ''}`}
          onClick={() => setActiveTab('guide')}
        >
          使用说明
        </button>
      </div>
      {/* 三个页面都保持挂载，仅切换显隐：切标签不丢失各自的状态与进行中的任务 */}
      <div style={{ display: activeTab === 'match' ? 'block' : 'none' }}>
        <MatchingPage />
      </div>
      <div style={{ display: activeTab === 'doc' ? 'block' : 'none' }}>
        <InspectionDocPage />
      </div>
      <div style={{ display: activeTab === 'guide' ? 'block' : 'none' }}>
        <UsageGuidePage />
      </div>
    </div>
  )
}

const SAMPLE_FILES = ['20C114582-Y.png', '20C114257-Y.png', '20C114820-Y.png']

function UsageGuidePage() {
  return (
    <div className="guide-page">
      <section className="guide-section">
        <h2>功能概览</h2>
        <ul className="guide-list">
          <li>
            <b>图纸检索</b>：上传一张局部零件图片（或技术参数图片），系统分别从"图形相似性"和"文本语义接近度"两个维度，
            在图纸知识库中检索最相似的图纸，给出排名前 3 的候选结果，并实时展示 AI 逐图纸比对的完整思考过程与各阶段耗时；
            排名列表中鼠标悬浮在图纸名称上，还能弹出「上传图片 vs 原始 PDF」并排预览，方便核对。
          </li>
          <li>
            <b>文档生成</b>：选择一张已入库的图纸，AI 基于图纸的解析内容，按可编辑的抽取规则自动识别产品信息与检验项目，
            组装成结构化数据供人工核对/修改，确认后一键套用模版生成《制品半成品检验作业指导书》Word 文档。
          </li>
        </ul>
      </section>

      <section className="guide-section">
        <h2>使用步骤</h2>
        <div className="guide-subsection">
          <h3>图纸检索</h3>
          <ol>
            <li>登录后进入"图纸检索"标签页。</li>
            <li>点击选择文件，上传一张局部零件图片（支持 png/jpg/jpeg/bmp）。</li>
            <li>按需拖动滑块调整"图片权重／文本权重"，决定两个维度在综合得分中的占比。</li>
            <li>点击"上传并匹配"，页面会实时展示 AI 思考过程（判定模式、向量化进度、并行比对通道）。</li>
            <li>匹配完成后查看排名前 3 的图纸；鼠标悬浮在图纸名称上可弹出并排预览面板。</li>
          </ol>
        </div>
        <div className="guide-subsection">
          <h3>文档生成</h3>
          <ol>
            <li>切换到"文档生成 - 生产检验指导书"标签页。</li>
            <li>从下拉框中选择目标图纸，可展开"信息提取规则"按需调整抽取逻辑。</li>
            <li>点击"内容识别"，等待 AI 抽取完成。</li>
            <li>在下方预览表格中逐项核对/修改抽取结果，勾选要插入文档的产品图片。</li>
            <li>点击"生成文档"，浏览器会自动下载生成好的 .docx 文件。</li>
          </ol>
        </div>
      </section>

      <section className="guide-section">
        <h2>样例数据下载</h2>
        <p className="hint">可以下载下面几张局部零件图片，直接用于"图纸检索"功能的试用。</p>
        <div className="sample-list">
          {SAMPLE_FILES.map((name) => (
            <a key={name} className="sample-item" href={`/static/samples/${name}`} download={name}>
              <img src={`/static/samples/${name}`} alt={name} loading="lazy" />
              <span>{name}</span>
            </a>
          ))}
        </div>
      </section>
    </div>
  )
}

const PRODUCT_FIELDS = ['产品图号', '产品名称', '产品规格', '材料', '产品颜色', '产品净重', '顾客代码', '编制', '日期']
const INSP_COLS = ['序号', '检验项目', '特性标识', '检验设施/器具', '检验频次']

// 把提示词文本渲染成带层级、项名加粗的只读视图（不改提示词本身，发给后端的仍是原文）。
// 规则：以「·/•」开头的行视为子项并缩进；每行（或子项）首个「：」前若是简短标签（无句读、≤16 字），则加粗。
function PromptView({ text }) {
  const lines = (text || '').split('\n')
  return (
    <div className="prompt-view">
      {lines.map((raw, i) => {
        if (!raw.trim()) return <div key={i} className="pv-gap" />
        const sub = /^\s*[·•]\s*/.test(raw)
        const body = sub ? raw.replace(/^\s*[·•]\s*/, '') : raw.trim()
        const idx = body.indexOf('：')
        let label = ''
        let rest = body
        if (idx > 0 && idx <= 16 && !/[。，、；,.;]/.test(body.slice(0, idx))) {
          label = body.slice(0, idx)
          rest = body.slice(idx)
        }
        return (
          <p key={i} className={sub ? 'pv-sub' : 'pv-item'}>
            {label && <strong>{label}</strong>}
            {rest}
          </p>
        )
      })}
    </div>
  )
}

function InspectionDocPage() {
  const [diagrams, setDiagrams] = useState([])
  const [diagram, setDiagram] = useState('')
  const [prompt, setPrompt] = useState('')
  const [editPrompt, setEditPrompt] = useState(false) // false=格式化预览，true=编辑
  const [extracting, setExtracting] = useState(false)
  const [data, setData] = useState(null) // 统一大 JSON
  const [images, setImages] = useState([]) // 该图纸全部图片相对路径
  const [selectedImages, setSelectedImages] = useState([]) // 勾选的图片
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('') // 抽取阶段错误（顶部）
  const [genError, setGenError] = useState('') // 生成阶段错误（按钮下方，更醒目）

  const DEFAULT_DIAGRAM = '20C114800.pdf'

  useEffect(() => {
    fetch('/api/diagrams')
      .then((r) => r.json())
      .then((d) => {
        const list = d.diagrams || []
        setDiagrams(list)
        if (list.length) {
          setDiagram(list.includes(DEFAULT_DIAGRAM) ? DEFAULT_DIAGRAM : list[0])
        }
      })
      .catch(() => setError('加载图纸列表失败'))
    fetch('/api/doc_prompt')
      .then((r) => r.json())
      .then((d) => setPrompt(d.prompt || ''))
      .catch(() => {})
  }, [])

  async function handleExtract() {
    if (!diagram) {
      setError('请先选择图纸')
      return
    }
    setExtracting(true)
    setError('')
    setData(null)
    setImages([])
    setSelectedImages([])
    try {
      const fd = new FormData()
      fd.append('diagram', diagram)
      fd.append('prompt', prompt)
      const resp = await fetch('/api/extract_doc_fields', { method: 'POST', body: fd })
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
      const j = await resp.json()
      setData(j.data || null)
      const ir = await fetch('/api/diagram_images?diagram=' + encodeURIComponent(diagram))
      const idata = await ir.json()
      setImages(idata.images || [])
    } catch (e) {
      setError(String(e))
    } finally {
      setExtracting(false)
    }
  }

  function mutate(fn) {
    setData((d) => {
      const nd = structuredClone(d)
      fn(nd)
      return nd
    })
  }
  const updateTop = (key, val) => mutate((d) => { d[key] = val })
  const updateInsp = (ci, ri, col, val) => mutate((d) => { d['检验项目'][ci]['明细'][ri][col] = val })
  function addRow(ci) {
    mutate((d) => {
      const rows = d['检验项目'][ci]?.['明细']
      if (!rows) return
      rows.push({ 序号: '', 检验项目: '', 特性标识: '', '检验设施/器具': '', 检验频次: '' })
      rows.forEach((r, i) => { r['序号'] = i + 1 })
    })
  }
  function removeRow(ci, ri) {
    mutate((d) => {
      const rows = d['检验项目'][ci]?.['明细']
      if (!rows) return
      rows.splice(ri, 1)
      rows.forEach((r, i) => { r['序号'] = i + 1 })
    })
  }

  function toggleImage(rel) {
    setGenError('')
    setSelectedImages((prev) =>
      prev.includes(rel) ? prev.filter((x) => x !== rel) : [...prev, rel]
    )
  }

  async function handleGenerate() {
    if (selectedImages.length === 0) {
      setGenError('请至少选择一张“产品图片+尺寸简图”')
      return
    }
    setGenerating(true)
    setGenError('')
    try {
      const fd = new FormData()
      fd.append('diagram', diagram)
      fd.append('data', JSON.stringify(data))
      fd.append('images', JSON.stringify(selectedImages))
      const resp = await fetch('/api/generate_doc', { method: 'POST', body: fd })
      if (!resp.ok) throw new Error('HTTP ' + resp.status)
      const blob = await resp.blob()
      const stem = diagram.replace(/\.pdf$/i, '')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${stem}_工序检验作业指导书.docx`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setGenError(String(e))
    } finally {
      setGenerating(false)
    }
  }

  const imagePicker = (
    <div className="dr-imgpick">
      <div className="dr-imgtitle">产品图片+尺寸简图（勾选要插入的图片）</div>
      {images.length === 0 ? (
        <div className="hint">该图纸无可选图片</div>
      ) : (
        <div className="image-grid">
          {images.map((rel) => (
            <label
              key={rel}
              className={`image-cell ${selectedImages.includes(rel) ? 'selected' : ''}`}
            >
              <input
                type="checkbox"
                checked={selectedImages.includes(rel)}
                onChange={() => toggleImage(rel)}
              />
              <img src={`/static/diagrams/${rel}`} alt="" loading="lazy" />
            </label>
          ))}
        </div>
      )}
    </div>
  )

  return (
    <div className="doc-page">
      <p className="hint">
        使用步骤：选择图纸并确认提取规则 → 内容识别完成后在下方"文档预览"里逐项核对/修改、勾选图片 → 确认生成文档。
      </p>
      <div className="doc-form">
        <label className="doc-field">
          <span>选择图纸</span>
          <select value={diagram} onChange={(e) => setDiagram(e.target.value)} disabled={extracting}>
            {diagrams.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
        </label>
        <div className="doc-field">
          <span className="prompt-caption">
            信息提取规则（可根据图纸的实际内容调整）
            <button
              type="button"
              className="prompt-toggle"
              onClick={() => setEditPrompt((v) => !v)}
              disabled={extracting}
            >
              {editPrompt ? '完成' : '编辑'}
            </button>
          </span>
          {editPrompt ? (
            <textarea rows={16} value={prompt} onChange={(e) => setPrompt(e.target.value)} disabled={extracting} />
          ) : (
            <PromptView text={prompt} />
          )}
        </div>
        <button className="search-btn" onClick={handleExtract} disabled={extracting}>
          {extracting ? '提取中…' : '内容识别'}
        </button>
      </div>

      {error && <div className="error">出错：{error}</div>}

      {data && (
        <div className="doc-result">
          <h2>预览/修改</h2>
          <div className="doc-replica">
            <div className="dr-title">XXXX 设计股份有限公司 制品半成品检验作业指导书</div>

            {/* 页眉块 */}
            <table className="dr-table">
              <tbody>
                <tr>
                  <td className="dr-label">文件编号</td>
                  <td><input value={data['文件编号'] ?? ''} onChange={(e) => updateTop('文件编号', e.target.value)} /></td>
                  <td className="dr-label">过程名称</td>
                  <td><input value={data['过程名称'] ?? ''} onChange={(e) => updateTop('过程名称', e.target.value)} /></td>
                  <td className="dr-label">版本编号</td>
                  <td><input value={data['版本编号'] ?? ''} onChange={(e) => updateTop('版本编号', e.target.value)} /></td>
                </tr>
                <tr>
                  <td className="dr-label">过程负责</td><td className="dr-readonly">工序质控员</td>
                  <td className="dr-label">过程代码</td><td className="dr-readonly">D01</td>
                  <td className="dr-label">发布日期</td>
                  <td><input value={data['发布日期'] ?? ''} onChange={(e) => updateTop('发布日期', e.target.value)} /></td>
                </tr>
              </tbody>
            </table>

            {/* 工序流程 */}
            <table className="dr-table">
              <tbody>
                <tr>
                  <td className="dr-label">工序流程</td>
                  <td><textarea rows={2} value={data['工序流程'] ?? ''} onChange={(e) => updateTop('工序流程', e.target.value)} /></td>
                </tr>
              </tbody>
            </table>

            {/* 产品信息块 + 图片 */}
            <table className="dr-table">
              <tbody>
                {PRODUCT_FIELDS.map((k, i) => (
                  <tr key={k}>
                    <td className="dr-label">{k}</td>
                    <td><input value={data[k] ?? ''} onChange={(e) => updateTop(k, e.target.value)} /></td>
                    {i === 0 && (
                      <td className="dr-imgcell" rowSpan={PRODUCT_FIELDS.length}>{imagePicker}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>

            {/* 检验项目表 */}
            <table className="dr-table dr-inspect">
              <thead>
                <tr>
                  <th>项目</th><th>序号</th><th>检验项目</th><th>特性标识</th>
                  <th>检验设施/器具</th><th>检验频次</th><th></th>
                </tr>
              </thead>
              <tbody>
                {(data['检验项目'] || []).map((cat, ci) => {
                  return cat['明细'].map((row, ri) => (
                    <tr key={`${ci}-${ri}`}>
                      {ri === 0 && (
                        <td className="dr-cat" rowSpan={cat['明细'].length}>
                          <div>{cat['项目']}</div>
                          <button className="dr-mini" type="button" onClick={() => addRow(ci)}>+ 行</button>
                        </td>
                      )}
                      {INSP_COLS.map((col) => (
                        <td key={col}>
                          <input
                            value={String(row[col] ?? '')}
                            onChange={(e) => updateInsp(ci, ri, col, e.target.value)}
                          />
                        </td>
                      ))}
                      <td className="dr-rowact">
                        <button className="dr-mini dr-del" type="button" title="删除该行" onClick={() => removeRow(ci, ri)}>×</button>
                      </td>
                    </tr>
                  ))
                })}
              </tbody>
            </table>
          </div>

          <button className="search-btn" onClick={handleGenerate} disabled={generating}>
            {generating ? '生成中…' : `生成文档（已选 ${selectedImages.length} 张图片）`}
          </button>
          {genError && <div className="gen-error">⚠ {genError}</div>}
        </div>
      )}
    </div>
  )
}

function GraphicResult({ result, pdfDiagrams }) {
  const top = result.top
  const uploadImage = result.upload_image
  return (
    <div className="result">
      <div className="meta">
        权重：图片 {pct(result.weights.image)}、文本 {pct(result.weights.text)}
        ｜相似度函数：{result.similarity_func}
      </div>

      {top && (
        <div className="top-card">
          <div className="top-head">
            <h2>
              最相似图纸：
              <DiagramHoverPreview
                diagramName={top.diagram_name}
                uploadImage={uploadImage}
                hasPdf={pdfDiagrams.has(top.diagram_name)}
              >
                {top.diagram_name}
              </DiagramHoverPreview>
            </h2>
            <span className="composite-score">
              综合得分：{top.composite.toFixed(4)}
            </span>
          </div>

          <div className="result-section image-section">
            <h3>图形相似性（片段图片 ↔ 图纸视图）</h3>
            <div className="pair-list">
              {top.image_matches.map((m, i) => (
                <div className="img-pair" key={i}>
                  <figure>
                    <img src={`/static/fragments/${m.source_ref}`} alt={m.source_name} />
                    <figcaption>片段：{m.source_name}</figcaption>
                  </figure>
                  <div className="pair-sim">
                    <span>↔</span>
                    <b>{m.similarity.toFixed(4)}</b>
                  </div>
                  <figure>
                    <img src={`/static/diagrams/${m.target_ref}`} alt={m.target_name} />
                    <figcaption>图纸：{m.target_name}</figcaption>
                  </figure>
                </div>
              ))}
            </div>
          </div>

          <div className="result-section text-section">
            <h3>文本内容接近度（片段分段 ↔ 图纸文本）</h3>
            <div className="pair-list">
              {top.text_matches.map((m, i) => (
                <TextPair
                  key={i}
                  head={`${m.source_name} ↔ ${m.target_name}`}
                  sim={m.similarity}
                  left={{ label: `片段：${m.source_name}`, text: m.source_text }}
                  right={{ label: `图纸：${m.target_name}`, text: m.target_text }}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      <RankTable
        ranking={result.ranking}
        cols={['图片得分×权重', '文本得分×权重', '综合得分']}
        cells={(r) => [
          r.image_component.toFixed(4),
          r.text_component.toFixed(4),
          <b key="c">{r.composite.toFixed(4)}</b>,
        ]}
        uploadImage={uploadImage}
        pdfDiagrams={pdfDiagrams}
      />
    </div>
  )
}

function TextResult({ result, pdfDiagrams }) {
  const top = result.top
  const uploadImage = result.upload_image
  return (
    <div className="result">
      <div className="meta">文本模式（仅文本维度）｜相似度函数：{result.similarity_func}</div>

      {top && (
        <div className="top-card">
          <h2>
            最相似图纸：
            <DiagramHoverPreview
              diagramName={top.diagram_name}
              uploadImage={uploadImage}
              hasPdf={pdfDiagrams.has(top.diagram_name)}
            >
              {top.diagram_name}
            </DiagramHoverPreview>
          </h2>
          <div>文本相似度：{top.similarity.toFixed(4)}</div>
          <h3>上传文本 ↔ 图纸文本</h3>
          <div className="pair-list">
            <TextPair
              head={`上传 md ↔ ${top.best_chunk_name}`}
              sim={top.similarity}
              left={{ label: '上传图片识别文本', text: result.source_text }}
              right={{ label: `图纸：${top.best_chunk_name}`, text: top.best_chunk_text }}
            />
          </div>
        </div>
      )}

      <RankTable
        ranking={result.ranking}
        cols={['最相似分段', '文本相似度']}
        cells={(r) => [r.best_chunk_name, <b key="s">{r.similarity.toFixed(4)}</b>]}
        uploadImage={uploadImage}
        pdfDiagrams={pdfDiagrams}
      />
    </div>
  )
}

// 各阶段耗时展示（匹配完成后，跟在「AI 思考过程」日志之后、比对通道之前）。
// 只展示本次实际跑过的阶段：命中已有片段产物时会跳过切图/解读，对应字段不会出现。
const STAGE_DEFS = [
  { key: 'paddle_crop', label: 'PaddleOCR 切图' },
  { key: 'vlm_interpret', label: 'VLM 解读内容' },
  { key: 'vectorize', label: '被检索内容向量化' },
  { key: 'mo_match', label: '向量相似度匹配' },
]

function fmtSecs(s) {
  return s < 1 ? `${Math.round(s * 1000)}ms` : `${s.toFixed(2)}s`
}

function StageTimings({ timings }) {
  const rows = STAGE_DEFS.filter((d) => typeof timings[d.key] === 'number')
  if (rows.length === 0) return null
  const total = rows.reduce((sum, d) => sum + timings[d.key], 0)
  return (
    <div className="stage-timings">
      <div className="stage-timings-title">各阶段耗时</div>
      <div className="stage-timings-grid">
        {rows.map((d) => (
          <div className="stage-timing-item" key={d.key}>
            <span className="stage-timing-label">{d.label}</span>
            <span className="stage-timing-value">{fmtSecs(timings[d.key])}</span>
          </div>
        ))}
        <div className="stage-timing-item stage-timing-total">
          <span className="stage-timing-label">合计</span>
          <span className="stage-timing-value">{fmtSecs(total)}</span>
        </div>
      </div>
    </div>
  )
}

function WorkerWindow({ index, lines, running }) {
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [lines])
  return (
    <div className="worker-box">
      <div className="worker-title">
        <span className={`worker-dot ${running ? 'active' : 'done'}`} />
        比对通道 <b>{index + 1}</b>
      </div>
      <pre ref={ref} className="worker-log">
        {lines.join('\n')}
      </pre>
    </div>
  )
}

function TextPair({ head, sim, left, right }) {
  return (
    <div className="text-pair">
      <div className="text-pair-head">
        {head}
        <b className="pair-sim-inline">{sim.toFixed(4)}</b>
      </div>
      <div className="text-cols">
        <div className="text-col">
          <div className="text-col-label">{left.label}</div>
          <div className="md-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{left.text}</ReactMarkdown>
          </div>
        </div>
        <div className="text-col">
          <div className="text-col-label">{right.label}</div>
          <div className="md-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{right.text}</ReactMarkdown>
          </div>
        </div>
      </div>
    </div>
  )
}

// 悬浮在图纸名称上时，弹出「上传图片 vs 原始 PDF」并排预览面板。
// uploadImage 为空（复用旧片段但从未真正走过上传接口）时退化为纯文本、不提供悬浮。
const HOVER_PANEL_W = 960
const HOVER_PANEL_H = 560

function DiagramHoverPreview({ diagramName, uploadImage, hasPdf, children }) {
  const [hover, setHover] = useState(false)
  const [box, setBox] = useState({ top: 0, left: 0, width: HOVER_PANEL_W, height: HOVER_PANEL_H })
  const ref = useRef(null)

  function handleEnter() {
    if (ref.current) {
      const rect = ref.current.getBoundingClientRect()
      // 面板尺寸也按视口裁一遍：窗口比面板本身还小时（较小的浏览器窗口/分屏）
      // 只调整 position 不够，还得把面板本身缩小，否则会溢出视口。
      const width = Math.min(HOVER_PANEL_W, window.innerWidth - 24)
      const height = Math.min(HOVER_PANEL_H, window.innerHeight - 24)
      let left = rect.left
      left = Math.min(left, window.innerWidth - width - 12)
      left = Math.max(12, left)
      let top = rect.bottom + 6
      if (top + height > window.innerHeight - 12) {
        top = Math.max(12, rect.top - height - 6) // 下方放不下就翻到上方
      }
      setBox({ top, left, width, height })
    }
    setHover(true)
  }

  if (!uploadImage) return <>{children}</>

  return (
    <span
      ref={ref}
      className="diagram-hover-trigger"
      onMouseEnter={handleEnter}
      onMouseLeave={() => setHover(false)}
    >
      {children}
      {hover && (
        <div
          className="diagram-hover-panel"
          style={{ top: box.top, left: box.left, width: box.width, height: box.height }}
        >
          <div className="diagram-hover-col diagram-hover-col-upload">
            <div className="diagram-hover-label">上传图片</div>
            <div className="diagram-hover-body">
              <img src={`/static/upload/${uploadImage}`} alt="上传图片" />
            </div>
          </div>
          <div className="diagram-hover-col diagram-hover-col-pdf">
            <div className="diagram-hover-label">原始图纸：{diagramName}</div>
            <div className="diagram-hover-body">
              {hasPdf ? (
                <iframe
                  title={diagramName}
                  src={`/static/pdfs/${diagramName}#toolbar=0&navpanes=0&view=FitH`}
                />
              ) : (
                <div className="diagram-hover-nopdf">暂无该图纸的 PDF 文件</div>
              )}
            </div>
          </div>
        </div>
      )}
    </span>
  )
}

function RankTable({ ranking, cols, cells, uploadImage, pdfDiagrams }) {
  return (
    <>
      <h2>排名（Top {ranking.length}）</h2>
      <table className="rank-table">
        <thead>
          <tr>
            <th>排名</th>
            <th>图纸</th>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ranking.map((r) => (
            <tr key={r.diagram_name}>
              <td>{r.rank}</td>
              <td>
                <DiagramHoverPreview
                  diagramName={r.diagram_name}
                  uploadImage={uploadImage}
                  hasPdf={pdfDiagrams.has(r.diagram_name)}
                >
                  {r.diagram_name}
                </DiagramHoverPreview>
              </td>
              {cells(r).map((v, i) => (
                <td key={i}>{v}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
