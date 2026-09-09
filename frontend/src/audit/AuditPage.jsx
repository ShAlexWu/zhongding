import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle, ArrowLeft, ArrowRight, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight,
  CircleDashed, Clock3, Download, FileText, FolderOpen, Loader2, MinusCircle, Plus,
  Maximize2, RotateCw, Search, Trash2, Upload, X, XCircle, ZoomIn, ZoomOut,
} from 'lucide-react'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import './audit.css'

GlobalWorkerOptions.workerSrc = workerUrl

const BASE = '/api/audit/v1'
const SAMPLE_URL = '/static/samples/zhongji-v4-audit-sample.zip'
const STEPS = ['选择文件', '客户输入', '确认提交']
const KIND_LABELS = { pdf: '图纸PDF', api_json: '结构JSON', spatial: '空间JSON', docx: '说明书', trademark: '商标图' }
const VERDICTS = {
  pass: ['通过', 'ok'], fail: ['不通过', 'bad'], warning: ['警告', 'warn'], skipped: ['跳过', 'muted'],
  error: ['错误', 'bad'], pending: ['待审核', 'muted'],
}

async function api(path, init) {
  const response = await fetch(`${BASE}${path}`, init)
  const body = await response.json().catch(() => null)
  if (!response.ok || body?.code !== 0) throw new Error(body?.message || `请求失败（HTTP ${response.status}）`)
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

function formatBytes(value = 0) {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`
}

function IconButton({ label, children, ...props }) {
  return <button type="button" className="audit-icon-button" aria-label={label} title={label} {...props}>{children}</button>
}

function StatusBadge({ value }) {
  const map = {
    created: ['待开始', 'muted', CircleDashed], incomplete: ['资料不全', 'warn', AlertCircle],
    running: ['审核中', 'info', Loader2], aggregating: ['汇总中', 'info', Loader2],
    completed: ['已完成', 'ok', CheckCircle2], failed: ['已失败', 'bad', XCircle], interrupted: ['已中断', 'warn', MinusCircle],
  }
  const [label, tone, Icon] = map[value] || [value, 'muted', Clock3]
  return <span className={`audit-badge ${tone}`}><Icon className={value === 'running' || value === 'aggregating' ? 'spin' : ''} />{label}</span>
}

function VerdictBadge({ value }) {
  const [label, tone] = VERDICTS[value] || [value, 'muted']
  return <span className={`audit-badge ${tone}`}>{label}</span>
}

function VerdictIcon({ value }) {
  if (value === 'pass') return <CheckCircle2 className="verdict-icon ok" />
  if (value === 'fail' || value === 'error') return <XCircle className="verdict-icon bad" />
  if (value === 'warning') return <AlertCircle className="verdict-icon warn" />
  if (value === 'skipped') return <MinusCircle className="verdict-icon muted" />
  return <Clock3 className="verdict-icon muted" />
}

function FileChips({ files = [] }) {
  const kinds = [...new Set(files.map((file) => file.kind))]
  const bytes = files.reduce((sum, file) => sum + (file.size_bytes || 0), 0)
  return <div className="audit-file-chips">{kinds.map((kind) => <span key={kind}><FolderOpen />{KIND_LABELS[kind] || kind}</span>)}<small>{formatBytes(bytes)}</small></div>
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
    } catch (err) { setError(err.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    const timer = setTimeout(loadProjects, 0)
    fetch(`${BASE}/health`).then((response) => response.json()).then((body) => setHealth(body.data)).catch(() => {})
    return () => clearTimeout(timer)
  }, [loadProjects])

  async function removeProject(project) {
    if (!window.confirm(`确定删除任务“${project.name}”吗？`)) return
    try { await api(`/projects/${project.id}`, { method: 'DELETE' }); await loadProjects() }
    catch (err) { setError(err.message) }
  }

  if (selectedId) return <Review projectId={selectedId} onBack={() => { setSelectedId(''); loadProjects() }} />

  const running = projects.filter((project) => ['running', 'aggregating'].includes(project.status)).length
  const completed = projects.filter((project) => project.status === 'completed').length
  return (
    <div className="audit-page">
      <header className="audit-dashboard-head">
        <div className="audit-title-block">
          <h2>审图任务</h2>
          <p>40 条规则 × 8 域自动审核，附证据原文与图纸定位</p>
        </div>
        <div className="audit-stats" aria-label="任务统计">
          <span><strong>{projects.length}</strong>全部任务</span><i />
          <span><strong>{running}</strong>审核中</span><i />
          <span><strong>{completed}</strong>已完成</span>
        </div>
        <div className="audit-actions">
          <a className="audit-btn secondary" href={SAMPLE_URL} download><Download />下载样例</a>
          <button className="audit-btn primary" onClick={() => setDrawer(true)}><Plus />新建审图</button>
        </div>
      </header>

      <main className="audit-dashboard-body">
        {health?.vlm_dry_run && <div className="audit-notice" role="status"><AlertCircle />未配置审核模型 Key，当前为 dry-run；模型项只会标记为警告，不会产生真实 PASS。</div>}
        {error && <div className="audit-error"><AlertCircle />{error}</div>}
        <div className="audit-table-wrap">
          <table className="audit-table">
            <thead><tr><th>任务名称</th><th className="wide-only">文件</th><th>状态</th><th className="tablet-only">创建时间</th><th className="action-cell">操作</th></tr></thead>
            <tbody>
              {projects.map((project) => (
                <tr key={project.id} tabIndex="0" onClick={() => setSelectedId(project.id)} onKeyDown={(event) => event.key === 'Enter' && setSelectedId(project.id)}>
                  <td><div className="audit-task-name"><FileText /><span><b>{project.name || project.id}</b><small>{project.files?.length || 0} 个文件{project.finished_at ? ` · ${formatDate(project.finished_at)}` : ''}</small></span></div></td>
                  <td className="wide-only"><FileChips files={project.files} /></td>
                  <td><StatusBadge value={project.status} /></td>
                  <td className="tablet-only audit-date">{formatDate(project.created_at)}</td>
                  <td className="action-cell"><span className="audit-view-link">查看</span><IconButton label={`删除任务 ${project.name}`} disabled={['running', 'aggregating'].includes(project.status)} onClick={(event) => { event.stopPropagation(); removeProject(project) }}><Trash2 /></IconButton></td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && projects.length === 0 && <div className="audit-empty"><FileText /><b>暂无审图任务</b><span>下载样例数据，创建第一个任务</span></div>}
        </div>
        {loading && <p className="audit-loading"><Loader2 className="spin" />正在加载任务…</p>}
      </main>
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
    const next = Array.from(list).map((file) => ({ file, kind: classify(file) }))
    setFiles((current) => [...current, ...next].filter((item, index, all) => all.findIndex((candidate) => candidate.file.name === item.file.name && candidate.file.size === item.file.size) === index))
  }

  async function submit() {
    setSubmitting(true); setError('')
    const form = new FormData()
    form.append('name', name.trim()); form.append('tech_req', techReq.trim()); form.append('new_material', newMaterial.trim())
    form.append('revision_reason_type', reason); form.append('revision_reason_other_text', otherReason.trim())
    files.forEach((item) => { form.append('files', item.file); form.append('kinds', item.kind) })
    try {
      const response = await fetch(`${BASE}/projects/upload`, { method: 'POST', body: form })
      const body = await response.json().catch(() => null)
      if (!response.ok || body?.code !== 0) throw new Error(body?.message || `上传失败（HTTP ${response.status}）`)
      await api(`/projects/${body.data.id}/start`, { method: 'POST' })
      onCreated(body.data.id)
    } catch (err) { setError(err.message); setSubmitting(false) }
  }

  return (
    <div className="audit-drawer-layer">
      <button className="audit-drawer-mask" aria-label="关闭新建任务" onClick={() => !submitting && onClose()} />
      <aside className="audit-drawer" role="dialog" aria-modal="true" aria-labelledby="audit-drawer-title">
        <header><div><h2 id="audit-drawer-title">新建审图任务</h2><p>按次输入，不保存为客户主数据</p></div><IconButton label="关闭" onClick={onClose} disabled={submitting}><X /></IconButton></header>
        <div className="audit-stepper">
          {STEPS.map((label, index) => <span key={label}><button className={index === step ? 'active' : index < step ? 'done' : ''} disabled={index > step} onClick={() => index < step && setStep(index)}><b>{index + 1}</b>{label}</button>{index < STEPS.length - 1 && <i />}</span>)}
        </div>
        <div className="audit-drawer-body">
          {step === 0 && <>
            <h3>上传审核文件 <em>*</em></h3>
            <button className="audit-drop" onClick={() => inputRef.current?.click()} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files) }}><Upload /><strong>点击或拖拽文件到此处上传</strong><small>支持多文件同时上传 · PDF / JSON / DOCX</small></button>
            <input ref={inputRef} type="file" hidden multiple accept=".pdf,.json,.docx" onChange={(event) => addFiles(event.target.files)} />
            {files.length > 0 && <div className="audit-file-list">{files.map((item, index) => <div key={`${item.file.name}-${item.file.size}`}><FileText /><span title={item.file.name}><b>{item.file.name}</b><small>{formatBytes(item.file.size)}</small></span><select aria-label={`${item.file.name} 类型`} value={item.kind} onChange={(event) => setFiles((old) => old.map((file, i) => i === index ? { ...file, kind: event.target.value } : file))}>{Object.entries(KIND_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><IconButton label={`移除 ${item.file.name}`} onClick={() => setFiles((old) => old.filter((_, i) => i !== index))}><X /></IconButton></div>)}</div>}
            {invalidSize && <p className="audit-error"><AlertCircle />单文件不得超过 50 MiB，整批不得超过 200 MiB。</p>}
            <div className="audit-requirements"><AlertCircle /><div><strong>文件要求</strong><ul><li>6 套图纸 PDF、API JSON 与空间 JSON</li><li>1 个产品说明书 DOCX</li><li>1 个商标图 PDF；样例共 20 个文件</li></ul></div></div>
          </>}
          {step === 1 && <div className="audit-form">
            <label>客户技术要求 <em>*</em><textarea rows="5" value={techReq} onChange={(event) => setTechReq(event.target.value)} /></label>
            <label>新材料说明<textarea rows="3" value={newMaterial} onChange={(event) => setNewMaterial(event.target.value)} placeholder="没有可留空" /></label>
            <label>修订原因 <em>*</em><select value={reason} onChange={(event) => setReason(event.target.value)}><option>部门要求</option><option>标准更新</option><option>其他</option></select></label>
            {reason === '其他' && <label>原因说明<input value={otherReason} onChange={(event) => setOtherReason(event.target.value)} /></label>}
          </div>}
          {step === 2 && <div className="audit-confirm">
            <label>任务名称 <em>*</em><input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：20GP 21A-00 总装配出图审核" /></label>
            <dl><div><dt>审核文件</dt><dd>{files.length} 个，共 {formatBytes(total)}</dd></div><div><dt>客户技术要求</dt><dd>{techReq}</dd></div><div><dt>修订原因</dt><dd>{reason}{otherReason ? `：${otherReason}` : ''}</dd></div></dl>
          </div>}
          {error && <p className="audit-error"><AlertCircle />{error}</p>}
        </div>
        <footer><button className="audit-btn ghost" disabled={submitting} onClick={() => step === 0 ? onClose() : setStep(step - 1)}><ArrowLeft />{step === 0 ? '取消' : '上一步'}</button>{step < 2 ? <button className="audit-btn primary" disabled={!canNext} onClick={() => setStep(step + 1)}>下一步<ArrowRight /></button> : <button className="audit-btn primary" disabled={!canNext || submitting} onClick={submit}>{submitting && <Loader2 className="spin" />}{submitting ? '上传并创建中…' : '创建并开始审核'}</button>}</footer>
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
  const [filter, setFilter] = useState('all')
  const [collapsed, setCollapsed] = useState(new Set())
  const [error, setError] = useState('')
  const [rerunning, setRerunning] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [project, results] = await Promise.all([api(`/projects/${projectId}`), api(`/projects/${projectId}/rules`)])
      setDetail(project); setRules(results.items || [])
      setSelectedRule((current) => current || results.items?.[0]?.rule_key || '')
      setSelectedFile((current) => current || project.project.files?.find((file) => ['pdf', 'trademark'].includes(file.kind))?.id || '')
      setError('')
    } catch (err) { setError(err.message) }
  }, [projectId])

  useEffect(() => { const timer = setTimeout(refresh, 0); return () => clearTimeout(timer) }, [refresh])
  useEffect(() => {
    if (!detail || !['running', 'aggregating'].includes(detail.project.status)) return undefined
    const stream = new EventSource(`${BASE}/projects/${projectId}/events`)
    let timer
    const update = () => { clearTimeout(timer); timer = setTimeout(refresh, 150) }
    stream.addEventListener('rule_done', update); stream.addEventListener('task_status', update); stream.onerror = update
    return () => { clearTimeout(timer); stream.close() }
  }, [detail, projectId, refresh])

  const summary = detail?.summary || {}
  const filteredRules = useMemo(() => rules.filter((rule) => {
    const queryMatch = `${rule.rule_key}${rule.title}${rule.domain}`.toLowerCase().includes(search.toLowerCase())
    const verdictMatch = filter === 'all' || (filter === 'warning' ? ['warning', 'pending'].includes(rule.verdict) : rule.verdict === filter)
    return queryMatch && verdictMatch
  }), [filter, rules, search])
  const groupedRules = useMemo(() => {
    const groups = new Map()
    filteredRules.forEach((rule) => { if (!groups.has(rule.domain)) groups.set(rule.domain, []); groups.get(rule.domain).push(rule) })
    return [...groups.entries()]
  }, [filteredRules])
  const drawingGroups = groupedRules.filter(([domain]) => domain !== '全局通用')
  const globalGroup = groupedRules.find(([domain]) => domain === '全局通用')

  if (!detail) return <div className="audit-loading"><Loader2 className="spin" />{error || '正在加载审核结果…'}</div>
  const project = detail.project
  const current = rules.find((rule) => rule.rule_key === selectedRule) || rules[0]
  const drawings = project.files.filter((file) => file.kind === 'pdf')
  const manuals = project.files.filter((file) => file.kind === 'docx')
  const trademarks = project.files.filter((file) => file.kind === 'trademark')
  const previewFiles = [drawings[0], ...manuals, ...trademarks, ...drawings.slice(1)].filter(Boolean)
  const terminal = rules.filter((rule) => rule.verdict !== 'pending').length

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

  function toggleDomain(domain) {
    setCollapsed((currentSet) => { const next = new Set(currentSet); next.has(domain) ? next.delete(domain) : next.add(domain); return next })
  }

  return <div className="audit-review">
    <header className="audit-review-head">
      <button className="audit-back" onClick={onBack}><ArrowLeft />返回任务列表</button><i />
      <div className="audit-review-title"><h2>{project.name}</h2><span>{formatDate(project.created_at)}</span></div>
      <StatusBadge value={project.status} />
      <button className="audit-btn secondary export" onClick={report}><Download />导出批注 PDF</button>
    </header>
    {error && <div className="audit-error"><AlertCircle />{error}</div>}
    <div className="audit-review-grid">
      <PdfViewer files={previewFiles} fileId={selectedFile} onFile={setSelectedFile} page={page} onPage={setPage} rect={rect} />
      <section className="audit-rule-panel">
        <div className="audit-progress"><div><span>审核进度</span><b>终态 {terminal}/{rules.length || 40}</b></div><div className="audit-progress-bar"><i className="pass" style={{ width: `${(summary.pass || 0) / (rules.length || 1) * 100}%` }} /><i className="fail" style={{ width: `${(summary.fail || 0) / (rules.length || 1) * 100}%` }} /><i className="warning" style={{ width: `${(summary.warning || 0) / (rules.length || 1) * 100}%` }} /><i className="skipped" style={{ width: `${(summary.skipped || 0) / (rules.length || 1) * 100}%` }} /></div><p><span className="ok">{summary.pass || 0} 通过</span><span className="bad">{summary.fail || 0} 不通过</span><span className="warn">{(summary.warning || 0) + (summary.pending || 0)} 待审</span><span>{summary.skipped || 0} 不适用</span></p></div>
        <div className="audit-rule-tools"><div>{[['all', '全部'], ['fail', '不通过'], ['warning', '待审']].map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div><label><Search /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索规则…" /></label></div>
        <div className="audit-rules">{drawingGroups.map(([domain, items]) => <RuleGroup key={domain} domain={domain} items={items} collapsed={collapsed.has(domain)} currentKey={current?.rule_key} onToggle={toggleDomain} onSelect={(ruleKey) => { setSelectedRule(ruleKey); setRect(null); setActiveEvidence(-1) }} />)}</div>
        {globalGroup && <div className="audit-global-rules"><RuleGroup domain={globalGroup[0]} items={globalGroup[1]} collapsed={collapsed.has(globalGroup[0])} currentKey={current?.rule_key} onToggle={toggleDomain} onSelect={(ruleKey) => { setSelectedRule(ruleKey); setRect(null); setActiveEvidence(-1) }} /></div>}
      </section>
      <EvidencePanel current={current} files={previewFiles} activeEvidence={activeEvidence} jump={jump} rerun={rerun} rerunning={rerunning} />
    </div>
  </div>
}

function RuleGroup({ domain, items, collapsed, currentKey, onToggle, onSelect }) {
  return <div className="audit-rule-group">
    <button className="audit-domain" onClick={() => onToggle(domain)}>{collapsed ? <ChevronRight /> : <ChevronDown />}<b>{domain}</b><span>{items.filter((rule) => rule.verdict === 'fail').length || ''}<small>{items.filter((rule) => rule.verdict === 'pass').length}/{items.length}</small></span></button>
    {!collapsed && <ul>{items.map((rule) => <li key={rule.rule_key}><button className={rule.rule_key === currentKey ? 'active' : ''} onClick={() => onSelect(rule.rule_key)}><VerdictIcon value={rule.verdict} /><span><b title={rule.title}>{rule.title}</b><small>{rule.rule_key}</small></span></button></li>)}</ul>}
  </div>
}

function EvidencePanel({ current, files, activeEvidence, jump, rerun, rerunning }) {
  if (!current) return <section className="audit-evidence-panel audit-no-selection"><FileText /><p>在左侧选择一个审核项，此处展示判定详情与证据原文</p></section>
  const canRerun = ['fail', 'warning', 'error'].includes(current.verdict)
  const previewable = new Set(files.map((file) => file.id))
  return <section className="audit-evidence-panel">
    <header><div><span>{current.rule_key}</span><small>{current.domain}</small><b>{current.engine === 'vlm' ? 'VLM' : '规则'}</b></div><h2>{current.title}</h2><div><VerdictBadge value={current.verdict} />{canRerun && <button className="audit-btn secondary small" disabled={rerunning} onClick={rerun}><RotateCw className={rerunning ? 'spin' : ''} />{rerunning ? '重跑中…' : '重跑此项'}</button>}</div></header>
    <div className="audit-evidence-body">
      <section><h3>判定结论</h3><p>{current.conclusion || current.error || '等待规则执行…'}</p></section>
      <section><h3>证据 <span>{current.evidence?.length || 0}</span></h3>{(current.evidence || []).length ? <div className="audit-evidence-cards">{current.evidence.map((item, index) => { const canLocate = previewable.has(item.file_id) && item.page && item.rect; return <button key={index} className={index === activeEvidence ? 'active' : ''} disabled={!canLocate} onClick={() => jump(item, index)}><span>证据 {index + 1}{item.page ? ` · 第 ${item.page} 页` : ''}</span><p>{item.text || '未提供证据原文'}</p><i>{canLocate ? '定位图纸 ↗' : '文字依据'}</i></button> })}</div> : <p className="audit-muted">暂无可展示证据</p>}</section>
      <section><h3>执行信息</h3><dl><div><dt>引擎</dt><dd>{current.engine === 'vlm' ? '视觉大模型' : '确定性规则'}</dd></div><div><dt>延迟</dt><dd>{current.latency_ms ? `${current.latency_ms} ms` : current.status === 'done' ? '已完成' : '等待完成'}</dd></div><div><dt>尝试次数</dt><dd>{current.attempts ?? '—'}</dd></div><div><dt>成本</dt><dd>{current.cost_cny != null ? `¥${Number(current.cost_cny).toFixed(3)}` : '—'}</dd></div></dl></section>
    </div>
  </section>
}

function PdfViewer({ files, fileId, onFile, page, onPage, rect }) {
  const canvasRef = useRef(null)
  const scrollRef = useRef(null)
  const [pages, setPages] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [box, setBox] = useState(null)
  const [error, setError] = useState('')
  const selected = files.find((file) => file.id === fileId)

  function fitWidth() {
    const canvas = canvasRef.current
    const scroller = scrollRef.current
    if (!canvas || !scroller || !canvas.width) return
    setZoom(Math.max(.5, Math.min(2, Number(((scroller.clientWidth - 48) / (canvas.width / zoom)).toFixed(2)))))
  }

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
        if (scroller) requestAnimationFrame(() => scroller.scrollTo({ left: Math.max(0, nextBox.left - scroller.clientWidth / 2), top: Math.max(0, nextBox.top - scroller.clientHeight / 2), behavior: 'smooth' }))
      } else setBox(null)
      setError('')
    }).catch((err) => !cancelled && setError(err.message))
    return () => { cancelled = true; task.destroy() }
  }, [fileId, onPage, page, rect, zoom])

  return <section className="audit-pdf-panel">
    <div className="audit-pdf-tabs">{files.map((file) => { const name = file.path.split(/[\\/]/).pop(); return <button key={file.id} className={file.id === fileId ? 'active' : ''} onClick={() => { onFile(file.id); onPage(1) }} title={name}><FileText /><span>{file.kind === 'docx' ? '说明书' : name.replace(/_api\.pdf$/i, '')}</span></button> })}</div>
    <div className="audit-pdf-tools"><span><IconButton label="上一页" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft /></IconButton><b>{page} <i>/ {pages}</i></b><IconButton label="下一页" disabled={page >= pages} onClick={() => onPage(page + 1)}><ChevronRight /></IconButton></span><em /><span><IconButton label="缩小" onClick={() => setZoom((value) => Math.max(.5, value - .1))}><ZoomOut /></IconButton><b>{Math.round(zoom * 100)}%</b><IconButton label="放大" onClick={() => setZoom((value) => Math.min(2, value + .1))}><ZoomIn /></IconButton><IconButton label="适应宽度" onClick={fitWidth}><Maximize2 /></IconButton></span></div>
    <div ref={scrollRef} className="audit-canvas-scroll">{selected ? <div className="audit-canvas"><canvas ref={canvasRef} />{box && <span className="audit-highlight" style={box} />}</div> : <p className="audit-muted">没有可预览的 PDF</p>}{error && <p className="audit-error">PDF 加载失败：{error}</p>}</div>
  </section>
}
