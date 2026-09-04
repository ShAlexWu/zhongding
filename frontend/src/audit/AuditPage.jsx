import { useCallback, useEffect, useRef, useState } from 'react'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import './audit.css'

GlobalWorkerOptions.workerSrc = workerUrl

const BASE = '/api/audit/v1'
const SAMPLE_URL = '/static/samples/zhongji-v4-audit-sample.zip'
const STEPS = ['选择文件', '客户输入', '确认提交']
const KIND_LABELS = {
  pdf: '图纸 PDF',
  api_json: 'API JSON',
  spatial: '空间 JSON',
  docx: '产品说明书',
  trademark: '商标 PDF',
}
const VERDICTS = {
  pass: ['通过', 'ok'],
  fail: ['不通过', 'bad'],
  warning: ['警告', 'warn'],
  skipped: ['跳过', 'muted'],
  error: ['错误', 'bad'],
  pending: ['待审核', 'muted'],
}

async function api(path, init) {
  const response = await fetch(`${BASE}${path}`, init)
  const body = await response.json().catch(() => null)
  if (!response.ok || body?.code !== 0) {
    throw new Error(body?.message || `请求失败（HTTP ${response.status}）`)
  }
  return body.data
}

function classify(file) {
  const name = file.name.toLowerCase()
  if (name.endsWith('.docx')) return 'docx'
  if (name.endsWith('_spatial.json')) return 'spatial'
  if (name.endsWith('.json')) return 'api_json'
  if (name.endsWith('_api.pdf')) return 'pdf'
  return 'trademark'
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function StatusBadge({ value }) {
  const map = {
    created: ['待开始', 'muted'], incomplete: ['资料不全', 'warn'], running: ['审核中', 'info'],
    aggregating: ['汇总中', 'info'], completed: ['已完成', 'ok'], failed: ['已失败', 'bad'],
    interrupted: ['已中断', 'warn'],
  }
  const [label, tone] = map[value] || [value, 'muted']
  return <span className={`audit-badge ${tone}`}>{label}</span>
}

function VerdictBadge({ value }) {
  const [label, tone] = VERDICTS[value] || [value, 'muted']
  return <span className={`audit-badge ${tone}`}>{label}</span>
}

export default function AuditPage() {
  const [projects, setProjects] = useState([])
  const [health, setHealth] = useState(null)
  const [drawer, setDrawer] = useState(false)
  const [selectedId, setSelectedId] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const loadProjects = useCallback(async () => {
    try {
      const data = await api('/projects?limit=100')
      setProjects(data.items || [])
      setError('')
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = setTimeout(loadProjects, 0)
    fetch(`${BASE}/health`).then((r) => r.json()).then((x) => setHealth(x.data)).catch(() => {})
    return () => clearTimeout(timer)
  }, [loadProjects])

  if (selectedId) {
    return <Review projectId={selectedId} onBack={() => { setSelectedId(''); loadProjects() }} />
  }

  const running = projects.filter((p) => ['running', 'aggregating'].includes(p.status)).length
  const completed = projects.filter((p) => p.status === 'completed').length
  return (
    <div className="audit-page">
      {health?.vlm_dry_run && (
        <div className="audit-notice" role="status">
          当前未配置审核模型 Key，已进入 dry-run：模型审核项只会标记为警告，不会产生真实 PASS。
        </div>
      )}
      <header className="audit-dashboard-head">
        <div>
          <h2>审图任务</h2>
          <p>40 条规则 × 9 个图纸域自动审核，附证据原文与图纸定位</p>
        </div>
        <div className="audit-stats" aria-label="任务统计">
          <span>全部任务<strong>{projects.length}</strong></span>
          <span>审核中<strong className="blue">{running}</strong></span>
          <span>已完成<strong className="green">{completed}</strong></span>
        </div>
        <div className="audit-actions">
          <a className="audit-btn secondary" href={SAMPLE_URL} download>⇩ 下载样例数据</a>
          <button className="audit-btn primary" onClick={() => setDrawer(true)}>新建审核任务</button>
        </div>
      </header>

      {error && <div className="audit-error">{error}</div>}
      <div className="audit-table-wrap">
        <table className="audit-table">
          <thead><tr><th>任务名称</th><th>文件</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            {projects.map((project) => (
              <tr key={project.id}>
                <td>{project.name || project.id}</td>
                <td>▣ {project.files?.length || 0} 个文件</td>
                <td><StatusBadge value={project.status} /></td>
                <td>{formatDate(project.created_at)}</td>
                <td><button className="audit-link" onClick={() => setSelectedId(project.id)}>查看{project.status === 'running' ? '进度' : '结果'}</button></td>
              </tr>
            ))}
            {!loading && projects.length === 0 && (
              <tr><td colSpan="5" className="audit-empty">还没有审核任务，下载样例后新建第一个任务。</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {loading && <p className="audit-empty">正在加载任务…</p>}
      {drawer && <CreateDrawer onClose={() => setDrawer(false)} onCreated={(id) => { setDrawer(false); setSelectedId(id) }} />}
    </div>
  )
}

function CreateDrawer({ onClose, onCreated }) {
  const [step, setStep] = useState(0)
  const [files, setFiles] = useState([])
  const [name, setName] = useState('')
  const [techReq, setTechReq] = useState('按中集 20GP 标准要求审核')
  const [newMaterial, setNewMaterial] = useState('')
  const [reason, setReason] = useState('部门要求')
  const [otherReason, setOtherReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    const closeOnEscape = (event) => event.key === 'Escape' && !submitting && onClose()
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose, submitting])

  const total = files.reduce((sum, item) => sum + item.file.size, 0)
  const invalidSize = files.some((item) => item.file.size > 50 * 1024 * 1024) || total > 200 * 1024 * 1024
  const canNext = step === 0 ? files.length > 0 && !invalidSize : step === 1 ? techReq.trim() && reason : name.trim()

  function addFiles(list) {
    setFiles(Array.from(list).map((file) => ({ file, kind: classify(file) })))
  }

  async function submit() {
    setSubmitting(true); setError('')
    const form = new FormData()
    form.append('name', name.trim())
    form.append('tech_req', techReq.trim())
    form.append('new_material', newMaterial.trim())
    form.append('revision_reason_type', reason)
    form.append('revision_reason_other_text', otherReason.trim())
    files.forEach((item) => { form.append('files', item.file); form.append('kinds', item.kind) })
    try {
      const response = await fetch(`${BASE}/projects/upload`, { method: 'POST', body: form })
      const body = await response.json().catch(() => null)
      if (!response.ok || body?.code !== 0) throw new Error(body?.message || `上传失败（HTTP ${response.status}）`)
      await api(`/projects/${body.data.id}/start`, { method: 'POST' })
      onCreated(body.data.id)
    } catch (err) {
      setError(err.message); setSubmitting(false)
    }
  }

  return (
    <div className="audit-drawer-layer">
      <button className="audit-drawer-mask" aria-label="关闭新建任务" onClick={() => !submitting && onClose()} />
      <aside className="audit-drawer" role="dialog" aria-modal="true" aria-labelledby="audit-drawer-title">
        <header><div><h2 id="audit-drawer-title">新建审核任务</h2><p>按次输入，不保存为客户主数据</p></div><button className="audit-close" onClick={onClose} disabled={submitting} aria-label="关闭">×</button></header>
        <div className="audit-stepper">
          {STEPS.map((label, index) => <span key={label} className={index === step ? 'active' : index < step ? 'done' : ''}><b>{index + 1}</b>{label}</span>)}
        </div>
        <div className="audit-drawer-body">
          {step === 0 && (
            <>
              <h3>上传审核文件 <i>*</i></h3>
              <button className="audit-drop" onClick={() => inputRef.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); addFiles(e.dataTransfer.files) }}>
                <span>⇧</span><strong>点击或拖拽文件到此处上传</strong><small>支持多文件同时上传 · PDF / JSON / DOCX</small>
              </button>
              <input ref={inputRef} type="file" hidden multiple accept=".pdf,.json,.docx" onChange={(e) => addFiles(e.target.files)} />
              {files.length > 0 && <div className="audit-file-list">{files.map((item, index) => <div key={`${item.file.name}-${index}`}><span title={item.file.name}>{item.file.name}</span><select aria-label={`${item.file.name} 类型`} value={item.kind} onChange={(e) => setFiles((old) => old.map((x, i) => i === index ? { ...x, kind: e.target.value } : x))}>{Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button aria-label={`移除 ${item.file.name}`} onClick={() => setFiles((old) => old.filter((_, i) => i !== index))}>×</button></div>)}</div>}
              {invalidSize && <p className="audit-error">单文件不得超过 50 MiB，整批不得超过 200 MiB。</p>}
              <div className="audit-requirements"><strong>ⓘ 文件要求</strong><ul><li>6 套图纸 PDF、API JSON 与空间 JSON</li><li>1 个产品说明书 DOCX</li><li>1 个商标图 PDF；样例共 20 个文件</li></ul></div>
            </>
          )}
          {step === 1 && (
            <div className="audit-form">
              <label>客户技术要求 *<textarea rows="5" value={techReq} onChange={(e) => setTechReq(e.target.value)} /></label>
              <label>新材料说明<textarea rows="3" value={newMaterial} onChange={(e) => setNewMaterial(e.target.value)} placeholder="没有可留空" /></label>
              <label>修订原因 *<select value={reason} onChange={(e) => setReason(e.target.value)}><option>部门要求</option><option>标准更新</option><option>其他</option></select></label>
              {reason === '其他' && <label>原因说明<input value={otherReason} onChange={(e) => setOtherReason(e.target.value)} /></label>}
            </div>
          )}
          {step === 2 && (
            <div className="audit-confirm">
              <label>任务名称 *<input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：20GP 21A-00 总装配出图审核" /></label>
              <dl><div><dt>审核文件</dt><dd>{files.length} 个，共 {(total / 1024 / 1024).toFixed(1)} MiB</dd></div><div><dt>客户技术要求</dt><dd>{techReq}</dd></div><div><dt>修订原因</dt><dd>{reason}{otherReason ? `：${otherReason}` : ''}</dd></div></dl>
            </div>
          )}
          {error && <p className="audit-error">{error}</p>}
        </div>
        <footer><button className="audit-btn secondary" disabled={submitting} onClick={() => step === 0 ? onClose() : setStep(step - 1)}>{step === 0 ? '取消' : '上一步'}</button>{step < 2 ? <button className="audit-btn primary" disabled={!canNext} onClick={() => setStep(step + 1)}>下一步</button> : <button className="audit-btn primary" disabled={!canNext || submitting} onClick={submit}>{submitting ? '上传并创建中…' : '创建并开始审核'}</button>}</footer>
      </aside>
    </div>
  )
}

function Review({ projectId, onBack }) {
  const [detail, setDetail] = useState(null)
  const [rules, setRules] = useState([])
  const [selectedRule, setSelectedRule] = useState('')
  const [selectedFile, setSelectedFile] = useState('')
  const [page, setPage] = useState(1)
  const [rect, setRect] = useState(null)
  const [activeEvidence, setActiveEvidence] = useState(-1)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [rerunning, setRerunning] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [project, results] = await Promise.all([api(`/projects/${projectId}`), api(`/projects/${projectId}/rules`)])
      setDetail(project); setRules(results.items || [])
      setSelectedRule((current) => current || results.items?.[0]?.rule_key || '')
      setSelectedFile((current) => current || project.project.files?.find((f) => ['pdf', 'trademark'].includes(f.kind))?.id || '')
      setError('')
    } catch (err) { setError(err.message) }
  }, [projectId])

  useEffect(() => {
    const timer = setTimeout(refresh, 0)
    return () => clearTimeout(timer)
  }, [refresh])
  useEffect(() => {
    if (!detail || !['running', 'aggregating'].includes(detail.project.status)) return undefined
    const stream = new EventSource(`${BASE}/projects/${projectId}/events`)
    let timer
    const update = () => { clearTimeout(timer); timer = setTimeout(refresh, 150) }
    stream.addEventListener('rule_done', update)
    stream.addEventListener('task_status', update)
    stream.onerror = () => update()
    return () => { clearTimeout(timer); stream.close() }
  }, [detail, projectId, refresh])

  if (!detail) return <div className="audit-empty">{error || '正在加载审核结果…'}</div>
  const project = detail.project
  const current = rules.find((rule) => rule.rule_key === selectedRule) || rules[0]
  const pdfFiles = project.files.filter((file) => ['pdf', 'trademark'].includes(file.kind))
  const filteredRules = rules.filter((rule) => `${rule.rule_key}${rule.title}${rule.domain}`.toLowerCase().includes(search.toLowerCase()))
  const summary = detail.summary || {}

  function jump(anchor, index) {
    if (!anchor.file_id || !anchor.page || !anchor.rect) return
    setSelectedFile(anchor.file_id); setPage(anchor.page); setRect(anchor.rect); setActiveEvidence(index)
  }

  async function rerun() {
    if (!current) return
    setRerunning(true)
    try { await api(`/projects/${projectId}/rules/${current.rule_key}/rerun`, { method: 'POST' }); await refresh() }
    catch (err) { setError(err.message) }
    finally { setRerunning(false) }
  }

  async function report() {
    const response = await fetch(`${BASE}/projects/${projectId}/report?format=pdf`)
    if (!response.ok) { setError('批注 PDF 导出失败'); return }
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a'); link.href = url; link.download = `审图批注-${project.name}.pdf`; link.click(); URL.revokeObjectURL(url)
  }

  return (
    <div className="audit-review">
      <header className="audit-review-head">
        <button className="audit-btn secondary" onClick={onBack}>← 返回任务列表</button>
        <h2>{project.name}</h2><StatusBadge value={project.status} />
        <div className="audit-summary">通过 <b className="green">{summary.pass || 0}</b> · 不通过 <b className="red">{summary.fail || 0}</b> · 警告 <b className="amber">{summary.warning || 0}</b> · 跳过 {summary.skipped || 0}</div>
        <button className="audit-btn secondary" onClick={report}>⇩ 导出批注 PDF</button>
      </header>
      {error && <div className="audit-error">{error}</div>}
      <div className="audit-review-grid">
        <PdfViewer files={pdfFiles} fileId={selectedFile} onFile={setSelectedFile} page={page} onPage={setPage} rect={rect} />
        <section className="audit-rule-panel">
          <h3>审核项列表</h3><input className="audit-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索规则 ID / 名称" />
          <div className="audit-rules">{filteredRules.map((rule) => <button key={rule.rule_key} className={rule.rule_key === current?.rule_key ? 'active' : ''} onClick={() => { setSelectedRule(rule.rule_key); setRect(null); setActiveEvidence(-1) }}><span>{rule.rule_key}</span><b>{rule.title}</b><VerdictBadge value={rule.verdict} /></button>)}</div>
        </section>
        <section className="audit-evidence-panel">
          <h3>证据详情</h3>
          {current ? <><div className="audit-rule-title"><span>{current.rule_key} · {current.domain}</span><h2>{current.title}</h2><VerdictBadge value={current.verdict} /></div><div className="audit-conclusion"><strong>审核结论</strong><p>{current.conclusion || current.error || '等待规则执行…'}</p></div><div className="audit-evidence"><strong>证据</strong>{(current.evidence || []).length ? current.evidence.map((item, index) => <button key={index} className={index === activeEvidence ? 'active' : ''} disabled={!item.file_id || !item.page || !item.rect} onClick={() => jump(item, index)}><span>证据 {index + 1}{item.page ? ` · 第 ${item.page} 页` : ''}</span><p>{item.text || '未提供证据原文'}</p><i>{item.file_id && item.page && item.rect ? '定位 ↗' : '文字依据'}</i></button>) : <p className="audit-muted">暂无可展示证据</p>}</div><div className="audit-source"><strong>执行信息</strong><p>{current.engine === 'vlm' ? '视觉大模型' : '确定性规则'} · {current.latency_ms ? `${current.latency_ms} ms` : current.status === 'done' ? '已完成' : '等待完成'}</p></div>{['fail', 'warning', 'error'].includes(current.verdict) && <button className="audit-btn primary rerun" disabled={rerunning} onClick={rerun}>{rerunning ? '重跑中…' : '↻ 重跑此项'}</button>}</> : <p className="audit-muted">请选择审核项</p>}
        </section>
      </div>
    </div>
  )
}

function PdfViewer({ files, fileId, onFile, page, onPage, rect }) {
  const canvasRef = useRef(null)
  const scrollRef = useRef(null)
  const [pages, setPages] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [box, setBox] = useState(null)
  const [error, setError] = useState('')
  const selected = files.find((file) => file.id === fileId)

  useEffect(() => {
    if (!fileId || !canvasRef.current) return undefined
    let cancelled = false
    const task = getDocument({ url: `${BASE}/files/${fileId}`, withCredentials: true })
    task.promise.then(async (doc) => {
      if (cancelled) return
      setPages(doc.numPages)
      const safePage = Math.min(Math.max(page, 1), doc.numPages)
      if (safePage !== page) onPage(safePage)
      const pdfPage = await doc.getPage(safePage)
      const viewport = pdfPage.getViewport({ scale: zoom })
      const canvas = canvasRef.current
      canvas.width = viewport.width; canvas.height = viewport.height
      await pdfPage.render({ canvasContext: canvas.getContext('2d'), viewport }).promise
      if (rect) {
        const start = viewport.convertToViewportPoint(rect.x, rect.y)
        const end = viewport.convertToViewportPoint(rect.x + rect.w, rect.y + rect.h)
        const nextBox = { left: Math.min(start[0], end[0]), top: Math.min(start[1], end[1]), width: Math.abs(end[0] - start[0]), height: Math.abs(end[1] - start[1]) }
        setBox(nextBox)
        const scroller = scrollRef.current
        if (scroller) scroller.scrollTo({ left: Math.max(0, nextBox.left - scroller.clientWidth / 2), top: Math.max(0, nextBox.top - scroller.clientHeight / 2), behavior: 'smooth' })
      } else setBox(null)
      setError('')
    }).catch((err) => !cancelled && setError(err.message))
    return () => { cancelled = true; task.destroy() }
  }, [fileId, onPage, page, rect, zoom])

  return (
    <section className="audit-pdf-panel">
      <div className="audit-pdf-tools"><select value={fileId} onChange={(e) => { onFile(e.target.value); onPage(1) }}>{files.map((file) => <option key={file.id} value={file.id}>{file.path.split(/[\\/]/).pop()}</option>)}</select><span>页码 <button disabled={page <= 1} onClick={() => onPage(page - 1)}>−</button><b>{page} / {pages}</b><button disabled={page >= pages} onClick={() => onPage(page + 1)}>＋</button></span><span><button onClick={() => setZoom((x) => Math.max(.5, x - .1))}>−</button><b>{Math.round(zoom * 100)}%</b><button onClick={() => setZoom((x) => Math.min(2, x + .1))}>＋</button></span></div>
      <div ref={scrollRef} className="audit-canvas-scroll">{selected ? <div className="audit-canvas"><canvas ref={canvasRef} />{box && <span className="audit-highlight" style={box} />}</div> : <p className="audit-muted">没有可预览的 PDF</p>}{error && <p className="audit-error">PDF 加载失败：{error}</p>}</div>
    </section>
  )
}
