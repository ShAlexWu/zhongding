import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle, ArrowLeft, ArrowRight, CheckCircle2, ChevronDown, ChevronLeft, ChevronRight,
  CircleAlert, CircleDashed, Clock3, Download, FileText, FolderOpen, Loader2, MinusCircle, Plus,
  Crosshair, Maximize2, RotateCw, Search, Trash2, TriangleAlert, Upload, X, XCircle, ZoomIn, ZoomOut,
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
  pass: ['通过', 'ok', CheckCircle2], fail: ['不通过', 'bad', XCircle], warning: ['待审', 'warn', MinusCircle],
  skipped: ['不适用', 'muted', MinusCircle], error: ['错误', 'bad', TriangleAlert], pending: ['待审', 'muted', Clock3],
}
const DOMAIN_ORDER = ['说明书', '总图', '门端图', '侧板图', '前端图', '底架图', '顶板图', '商标图', '全局通用']
const DRAWING_NAMES = {
  '000A22G1B': '底架图', '000A22G1E': '门端图', '000A22G1F': '前端图',
  '000A22G1G': '总图', '000A22G1R': '顶板图', '000A22G1S': '侧板图',
}
const GROUPED_RULES = new Set(['GEN-01', 'GEN-02', 'GEN-03'])

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
  const [label, tone, Icon] = VERDICTS[value] || [value, 'muted', Clock3]
  return <span className={`audit-badge ${tone}`}><Icon />{label}</span>
}

function VerdictIcon({ value }) {
  if (value === 'pass') return <CheckCircle2 className="verdict-icon ok" />
  if (value === 'fail' || value === 'error') return <XCircle className="verdict-icon bad" />
  if (value === 'warning') return <AlertCircle className="verdict-icon warn" />
  if (value === 'skipped') return <MinusCircle className="verdict-icon muted" />
  return <Clock3 className="verdict-icon muted" />
}

function StarredBadge() {
  return <span className="audit-starred" title="重点项（审图清单标 * 项）"><TriangleAlert />重点</span>
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
  const [activeEvidence, setActiveEvidence] = useState(0)
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
    return DOMAIN_ORDER.map((domain) => [domain, filteredRules.filter((rule) => rule.domain === domain)]).filter(([, items]) => items.length)
  }, [filteredRules])

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
        <div className="audit-rule-tools"><div>{[['all', '全部'], ['fail', '不通过'], ['warning', '待审'], ['pass', '通过']].map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div><label><Search /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索规则…" /></label></div>
        <div className="audit-rules">{groupedRules.map(([domain, items]) => <RuleGroup key={domain} domain={domain} items={items} collapsed={collapsed.has(domain)} currentKey={current?.rule_key} onToggle={toggleDomain} onSelect={(ruleKey) => { setSelectedRule(ruleKey); setRect(null); setActiveEvidence(0) }} />)}</div>
      </section>
      <EvidencePanel current={current} files={previewFiles} activeEvidence={activeEvidence} jump={jump} rerun={rerun} rerunning={rerunning} />
    </div>
  </div>
}

function RuleGroup({ domain, items, collapsed, currentKey, onToggle, onSelect }) {
  const fail = items.filter((rule) => rule.verdict === 'fail').length
  const warning = items.filter((rule) => ['warning', 'pending'].includes(rule.verdict)).length
  const pass = items.filter((rule) => rule.verdict === 'pass').length
  return <div className="audit-rule-group">
    <button className="audit-domain" onClick={() => onToggle(domain)}>{collapsed ? <ChevronRight /> : <ChevronDown />}<b>{domain}</b><span>{fail > 0 && <em className="bad">{fail}</em>}{warning > 0 && <em className="warn">{warning}</em>}<small>{pass}/{items.length}</small></span></button>
    {!collapsed && <ul>{items.map((rule) => <li key={rule.rule_key}><button className={rule.rule_key === currentKey ? 'active' : ''} onClick={() => onSelect(rule.rule_key)}><VerdictIcon value={rule.verdict} /><span><b title={rule.title}>{rule.title}</b><small>{rule.rule_key}{rule.is_starred && <StarredBadge />}</small></span></button></li>)}</ul>}
  </div>
}

function EvidencePanel({ current, files, activeEvidence, jump, rerun, rerunning }) {
  const [selectedDrawing, setSelectedDrawing] = useState('')
  const [vlmOpen, setVlmOpen] = useState(false)
  if (!current) return <section className="audit-evidence-panel audit-no-selection"><FileText /><p>在左侧选择一个审核项，此处展示判定详情与证据原文</p></section>
  const canRerun = ['fail', 'warning', 'error'].includes(current.verdict)
  const drawingGroups = groupEvidenceByDrawing(current, files)
  const grouped = GROUPED_RULES.has(current.rule_key) && drawingGroups.length > 0
  const activeGroup = drawingGroups.find((group) => group.fileId === selectedDrawing) || drawingGroups[0]
  return <section className="audit-evidence-panel">
    <header>
      <div className="audit-evidence-meta"><span>{current.rule_key}</span><small>{current.domain}</small><i><b>{current.engine === 'vlm' ? 'VLM' : '规则'}</b>{current.priority === 'P0' && <em>P0</em>}</i></div>
      <h2>{current.title}</h2>
      <div className="audit-evidence-verdict"><VerdictBadge value={current.verdict} />{canRerun && <button className="audit-btn secondary small" disabled={rerunning} onClick={rerun}><RotateCw className={rerunning ? 'spin' : ''} />{rerunning ? '重跑中…' : '重跑此项'}</button>}</div>
      {grouped && <div className="audit-drawing-picker"><small>按图纸查看</small><div>{drawingGroups.map((group) => <button key={group.fileId} className={group.fileId === activeGroup.fileId ? 'active' : ''} aria-pressed={group.fileId === activeGroup.fileId} onClick={() => setSelectedDrawing(group.fileId)}><b>{group.label}</b><span>{group.code || `${group.entries.length} 条证据`}</span></button>)}</div></div>}
    </header>
    <div className="audit-evidence-body">
      {current.conclusion && <section><h3>判定结论</h3><p>{current.conclusion}</p></section>}
      {current.error && <div className="audit-rule-error"><AlertCircle />{current.error}</div>}
      {grouped && activeGroup && <section><h3>{activeGroup.label}证据 <span>({activeGroup.entries.length})</span></h3><GroupedEvidenceCards ruleKey={current.rule_key} entries={activeGroup.entries} activeEvidence={activeEvidence} jump={jump} /></section>}
      {!grouped && current.verdict !== 'pending' && <section><GenericEvidenceCards rule={current} activeEvidence={activeEvidence} jump={jump} /></section>}
      {current.vlm_raw && <section className="audit-vlm-raw"><button onClick={() => setVlmOpen((value) => !value)}>{vlmOpen ? <ChevronDown /> : <ChevronRight />}VLM 原始返回<span>{current.engine}</span></button>{vlmOpen && <pre>{JSON.stringify(current.vlm_raw, null, 2)}</pre>}</section>}
      <section><h3>执行信息</h3><dl><MetaRow label="Tokens in" value={current.tokens_in?.toLocaleString() || '—'} /><MetaRow label="Tokens out" value={current.tokens_out?.toLocaleString() || '—'} /><MetaRow label="成本 (CNY)" value={current.cost_cny != null ? `¥${Number(current.cost_cny).toFixed(3)}` : '—'} /><MetaRow label="延迟" value={current.latency_ms != null ? `${current.latency_ms} ms` : '—'} /><MetaRow label={current.engine === 'vlm' ? '模型尝试' : '人工重跑'} value={current.engine === 'vlm' ? current.vlm_attempts : current.attempts} /><MetaRow label="引擎" value={current.engine === 'vlm' ? '视觉大模型' : '规则引擎'} /></dl></section>
    </div>
  </section>
}

function MetaRow({ label, value }) {
  return <div><dt><Clock3 />{label}</dt><dd>{value ?? '—'}</dd></div>
}

const TYPE_LABEL = { doc: '说明书原文', json: 'JSON 字段', pdf: '图面区域', input: '客户输入' }

const CHECKPOINTS = {
  'MAN-01': [
    { label: '图号一致性', keywords: ['图号', 'Drawing No'] },
    { label: '版本号一致性', keywords: ['版本', 'Revision'] },
  ],
  'MAN-06': [{ label: '外面漆颜色' }, { label: '内面漆颜色' }],
  'TOT-01': [
    { label: '外部尺寸', basisIndexes: [0, 1, 2], drawingIndexes: [7, 8, 9], basisNote: '说明书外部长、宽、高', drawingNote: '总图外部长、宽、高标注' },
    { label: '内部尺寸', basisIndexes: [3, 4, 5], drawingIndexes: [10, 11, 12], basisNote: '说明书内部长、宽、高', drawingNote: '总图内部长、宽、高标注' },
    { label: '内部容积', basisIndexes: [6], drawingIndexes: [13], basisNote: '说明书额定内部容积', drawingNote: '依据总图内部尺寸计算' },
    { label: '外部容积', basisIndexes: [0, 1, 2], drawingIndexes: [7, 8, 9], basisNote: '依据说明书外部尺寸计算', drawingNote: '依据总图外部尺寸计算' },
  ],
  'TOT-02': [
    { label: '净重', keywords: ['tare_weight', '净重'] }, { label: '最大总重', keywords: ['max_gross', '总重'] },
    { label: '载重', keywords: ['payload', '载重'] }, { label: '堆码试验载荷', keywords: ['stacking', '堆码'] },
    { label: '地板强度', keywords: ['floor_strength', '地板强度'] },
  ],
  'TOT-03': [
    { label: '锁杆数量与门扇分布', keywords: ['锁杆'] }, { label: '通风器数量、侧板覆盖和端部位置', keywords: ['通风器'] },
    { label: '地板钉总数与每组 4/6 颗模式', keywords: ['地板钉'] }, { label: '地板钉纵向标准间距', keywords: ['地板钉'] },
  ],
  'TOT-04': [{ label: '变更板厚' }, { label: '客户特殊要求' }],
  'TOT-05': [
    { label: '前角柱拉筋排布与规格', keywords: ['前角柱'] }, { label: '后角柱拉筋排布与规格', keywords: ['后角柱'] },
    { label: '顶侧梁绳环排布与规格', keywords: ['顶侧梁'] }, { label: '底侧梁绳环排布与规格', keywords: ['底侧梁'] },
  ],
  'DOOR-01': [{ label: '门铰链选用', keywords: ['铰链'] }, { label: '门绳选用', keywords: ['门绳'] }, { label: '门封铆钉选用', keywords: ['铆钉'] }],
  'DOOR-02': [{ label: '门封胶条包角', keywords: ['包角', 'E100007'] }, { label: '门封压条 ABS 材质', keywords: ['压条', 'ABS'] }],
  'DOOR-06': [{ label: '锁杆整套或散件', keywords: ['锁杆'] }, { label: '后角柱拉筋排布', keywords: ['后角柱', '拉筋'] }],
  'SIDE-03': [{ label: '顶侧梁绳环排布与数量', keywords: ['绳环'] }, { label: '通风器排布与数量', keywords: ['通风器'] }],
  'FRONT-01': [{ label: '底角件三角板板厚', keywords: ['三角板'] }, { label: '鹅颈槽封板板厚', keywords: ['鹅颈槽', '封板'] }],
  'FRONT-02': [{ label: '前角柱拉筋排布', keywords: ['排布'] }, { label: '前角柱拉筋规格', keywords: ['材质', '板厚', '规格'] }],
  'FRONT-04': [{ label: '塑料地板支撑打胶注释', keywords: ['SEALING', '打胶'] }, { label: '塑料地板支撑多余焊接注释', keywords: ['WELD', '焊接'] }],
  'CHAS-03': [{ label: '宽／底横梁板厚', keywords: ['宽横梁', '底横梁'] }, { label: '短宽／底横梁板厚', keywords: ['短宽', '短底'] }],
  'CHAS-04': [{ label: '地板钉排布', keywords: ['地板钉'] }, { label: '前端避开塑料角撑', keywords: ['角撑', '地板支撑', 'F240102', 'F241002'] }],
  'TM-01': [
    { label: '门端视图', keywords: ['door_end'], basisNote: '对比两张切片的门板波形与通风器数量、位置。' },
    { label: '侧板视图', keywords: [' side:'], basisNote: '对比两张切片的侧板波形与通风器数量、位置。' },
    { label: '前端视图', keywords: ['front_end'], basisNote: '对比两张切片的前端波形与通风器数量、位置。' },
    { label: '顶板视图', keywords: [' roof:'], basisNote: '对比两张切片的顶板波形与通风器数量、位置。' },
  ],
  'TM-02': [{ label: 'ISO 标颜色' }, { label: '重量标颜色' }],
  'TM-04': [{ label: '重量标数值一致性' }, { label: '重量标格式' }],
  'TM-05': [{ label: '客户公司名称' }, { label: '客户公司地址' }],
  'TM-06': [
    { label: '允许堆码载荷（1.8g）', keywords: ['allowable_stacking_load_1_8g', '堆码'] },
    { label: '横向刚性试验力', keywords: ['transverse_racking_test_force', '横向刚性'] },
  ],
}

function buildGenericCheckpointCards(rule) {
  const evidenceCheckpoints = [...new Set((rule.evidence || []).map((anchor) => anchor.checkpoint).filter(Boolean))]
  const definitions = CHECKPOINTS[rule.rule_key] || (evidenceCheckpoints.length ? evidenceCheckpoints.map((label) => ({ label })) : [{ label: rule.title.replace(/^\*/, '') }])
  const entries = (rule.evidence || []).map((anchor, index) => ({ anchor, index }))
  const basisPool = entries.filter(({ anchor }) => anchor.side === 'manual' || anchor.type === 'input')
  const drawingPool = entries.filter(({ anchor }) => anchor.side !== 'manual' && anchor.type !== 'input')
  return definitions.map((definition) => {
    const checkpointEntries = entries.filter(({ anchor }) => anchor.checkpoint === definition.label)
    const select = (pool, indexes) => {
      if (indexes) return indexes.map((index) => entries[index]).filter(Boolean)
      if (checkpointEntries.length) return pool.filter((entry) => entry.anchor.checkpoint === definition.label)
      if (!definition.keywords) return pool
      return pool.filter(({ anchor }) => definition.keywords.some((keyword) => anchor.text.toLowerCase().includes(keyword.toLowerCase())))
    }
    return {
      label: definition.label,
      basis: select(basisPool, definition.basisIndexes),
      basisNote: definition.basisNote,
      drawing: select(drawingPool, definition.drawingIndexes),
      drawingNote: definition.drawingNote,
      verdict: checkpointEntries.find(({ anchor }) => anchor.checkpoint_verdict)?.anchor.checkpoint_verdict || rule.verdict,
    }
  })
}

function GenericEvidenceCards({ rule, activeEvidence, jump }) {
  return <div className="audit-checkpoint-cards">{buildGenericCheckpointCards(rule).map((card) => {
    const failed = ['fail', 'error'].includes(card.verdict)
    const review = ['warning', 'skipped'].includes(card.verdict) || card.drawing.length === 0
    const Icon = failed ? XCircle : review ? CircleAlert : CheckCircle2
    const status = failed ? (card.verdict === 'error' ? '执行失败' : '未通过') : review ? (card.verdict === 'skipped' ? '已跳过' : '待确认') : '已通过'
    const tone = failed ? 'fail' : review ? 'warning' : 'pass'
    return <article key={card.label} className={`${tone} ${card.drawing.some(({ index }) => index === activeEvidence) ? 'active' : ''}`}>
      <header><Icon /><b>{card.label}</b><span>{status}</span></header>
      <div className="audit-checkpoint-content">
        <div className="audit-checkpoint-section"><small>判定依据</small>{card.basisNote && <p>{card.basisNote}</p>}{card.basis.length ? <div>{card.basis.map((entry) => <EvidenceRow key={entry.index} label={TYPE_LABEL[entry.anchor.type] || '判定依据'} entry={entry} jump={jump} />)}</div> : !card.basisNote && <p>审核要求：{card.label}</p>}</div>
        <div className="audit-checkpoint-section"><small>{rule.domain === '说明书' ? '资料证据' : '图面证据'}</small>{card.drawingNote && <p>{card.drawingNote}</p>}{card.drawing.length ? <div className={rule.rule_key === 'TM-01' ? 'audit-evidence-grid' : ''}>{card.drawing.map((entry) => <EvidenceRow key={entry.index} label={rule.rule_key === 'TM-01' ? tm01EvidenceLabel(entry.anchor.text) : (TYPE_LABEL[entry.anchor.type] || '审查证据')} entry={entry} jump={jump} />)}</div> : <p className="audit-no-evidence">未找到与该审查点对应的证据。</p>}</div>
      </div>
    </article>
  })}</div>
}

function tm01EvidenceLabel(text) {
  const source = text.includes(' general ') ? '总图切片' : '商标图切片'
  if (text.startsWith('TM-01 WAVE ')) return `${source} · 波形`
  if (text.startsWith('TM-01 VENT ')) return `${source} · 通风器`
  return source
}

function GroupedEvidenceCards({ ruleKey, entries, activeEvidence, jump }) {
  if (ruleKey !== 'GEN-01') return <ProcessEvidenceCards entries={entries} activeEvidence={activeEvidence} jump={jump} />
  const cards = []
  entries.forEach((entry) => {
    const match = entry.anchor.text.match(/BOM 序号\s*(\d+).*?图号\s*([A-Z]\d{6}).*?厚度\/规格\s*([^；\s]+)/)
    if (match) cards.push({ bom: entry, item: match[1], partCode: match[2], thickness: match[3] })
    else if (cards.length && !cards.at(-1).drawing) cards.at(-1).drawing = entry
  })
  if (!cards.length) return <p className="audit-color-empty">未提取到可配对的 BOM 钣金件证据。</p>
  return <div className="audit-colored-cards">{cards.map((card) => {
    const missing = !card.drawing || card.drawing.anchor.text.includes('图中序号')
    const active = card.bom.index === activeEvidence || card.drawing?.index === activeEvidence
    return <article key={card.bom.index} className={`${missing ? 'fail' : 'pass'} ${active ? 'active' : ''}`}><header>{missing ? <XCircle /> : <CheckCircle2 />}<b>序号 {card.item}</b><code>{card.partCode}</code><span>{missing ? '缺少图面标注' : '标注一致'}</span></header><EvidenceRow label={`BOM 表 · ${card.thickness} mm`} entry={card.bom} jump={jump} />{card.drawing ? <EvidenceRow label={missing ? '图中序号定位' : '图面厚度标注'} entry={card.drawing} partCode={card.partCode} jump={jump} /> : <p className="audit-missing-evidence">图中未找到与 BOM 厚度一致且绑定到该零件的标注</p>}</article>
  })}</div>
}

function ProcessEvidenceCards({ entries, activeEvidence, jump }) {
  return <div className="audit-colored-cards">{entries.map((entry) => {
    const fields = Object.fromEntries(entry.anchor.text.split('｜').slice(1).map((field) => { const separator = field.indexOf('='); return separator < 0 ? [field, ''] : [field.slice(0, separator), field.slice(separator + 1)] }))
    const status = fields['状态'] || '已标注'
    const tone = ['缺失', '绑定无效'].includes(status) ? 'fail' : status === '待确认' ? 'warning' : 'pass'
    const Icon = tone === 'fail' ? XCircle : tone === 'warning' ? AlertCircle : CheckCircle2
    return <article key={entry.index} className={`${tone} ${entry.index === activeEvidence ? 'active' : ''}`}><header><Icon /><b>{fields['部位'] || '图面工艺标注'}</b><span>{status}</span></header><div className="audit-process-basis"><small>判定依据</small><p>{fields['依据'] || '图纸原有标注'}</p></div><EvidenceRow label="图面证据" entry={{ ...entry, anchor: { ...entry.anchor, text: fields['图面'] || entry.anchor.text } }} jump={jump} /></article>
  })}</div>
}

function groupEvidenceByDrawing(rule, files) {
  if (!GROUPED_RULES.has(rule.rule_key)) return []
  const fileById = new Map(files.map((file) => [file.id, file]))
  const groups = new Map()
  rule.evidence.forEach((anchor, index) => {
    const fileId = anchor.file_id || 'other'
    if (!groups.has(fileId)) {
      const fileName = fileById.get(fileId)?.path.split(/[\\/]/).pop() || ''
      const code = fileName.match(/000A22G1[A-Z]/)?.[0] || anchor.text.match(/000A22G1[A-Z]/)?.[0] || ''
      groups.set(fileId, { fileId, code, label: DRAWING_NAMES[code] || fileName.replace(/_api\.pdf$/i, '') || '其他证据', entries: [] })
    }
    groups.get(fileId).entries.push({ anchor, index })
  })
  return [...groups.values()]
}

function EvidenceRow({ label, entry, jump, partCode }) {
  const { anchor, index } = entry
  const locatable = ['pdf', 'doc'].includes(anchor.type) && anchor.file_id && anchor.page && anchor.rect
  return <div className="audit-paired-evidence"><div><div className="audit-evidence-label"><b>{label}</b>{anchor.page && <small>P{anchor.page}</small>}</div><p title={anchor.text}>{compactEvidence(anchor.text, partCode)}</p>{anchor.source_url && <a href={anchor.source_url} target="_blank" rel="noreferrer">官方来源{anchor.checked_at ? ` · ${anchor.checked_at}` : ''}</a>}</div>{locatable && <button onClick={() => jump(anchor, index)}><Crosshair />定位</button>}</div>
}

function compactEvidence(text, partCode) {
  if (/^TM-01 (VIEW|WAVE|VENT) /.test(text)) return text.slice(text.indexOf(':') + 1).trim().replace(/^(总图|商标图)切片：/, '')
  if (!text.includes('；绑定=')) return text
  const [value, bindings = ''] = text.split('；绑定=', 2)
  if (partCode) {
    const boundPart = bindings.split(' | ').find((name) => name.includes(partCode)) || bindings.split(' | ')[0]
    return boundPart ? `${value}；绑定=${boundPart.trim()}` : value
  }
  const names = [...new Set(bindings.split(' | ').map((item) => item.split('/').pop()?.trim()).filter(Boolean))]
  return `${value}；绑定=${names.slice(0, 3).join(' / ')}${names.length > 3 ? ` 等${names.length}处` : ''}`
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
