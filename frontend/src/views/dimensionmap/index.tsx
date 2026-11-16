import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/dimensionmap'
const columns = [
  "规则编号",
  "原始维度名",
  "标准维度名",
  "映射类型",
  "值转换逻辑",
  "默认取值",
  "映射优先级",
  "最近更新时间"
]
const editableFields = [
  "标准维度名",
  "映射类型",
  "匹配值",
  "值转换逻辑",
  "默认取值",
  "映射优先级"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]
const statuses = [
  "草稿",
  "规则评审中",
  "已生效",
  "已废弃"
]

type Row = Record<string, string | number | boolean | null>
type Tab = 'rules' | 'dataset' | 'versions'

type Summary = {
  "规则总数"?: number
  "已生效规则"?: number
  "评审中规则"?: number
  "当前版本"?: string | null
  "数据集行数"?: number
  "默认命中行数"?: number
  "同级冲突值数"?: number
  "未命中规则值数"?: number
  "当前版本映射版本"?: number | null
  "冲突处理策略"?: string
}

type VersionRow = {
  "映射版本": number
  "版本标识": string
  "状态": string
  "规则数": number
  "生效规则数": number
  "数据集行数": number
  "触发类型": string
  "触发动作": string
  "创建时间": string
  "被替代时间": string | null
}

export default function DimensionmapView() {
  const [tab, setTab] = useState<Tab>('rules')
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<Summary>({})
  const [dataset, setDataset] = useState<Row[]>([])
  const [datasetTotal, setDatasetTotal] = useState(0)
  const [versions, setVersions] = useState<VersionRow[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editValues, setEditValues] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [list, stats] = await Promise.all([
        fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`),
        fetchJson<Summary>(`${ENDPOINT}/summary`),
      ])
      setRows(list.items ?? [])
      setTotal(list.total ?? 0)
      setSummary(stats)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射列表读取失败')
    }
  }, [filters])

  const reloadDataset = useCallback(async () => {
    setErrorMessage('')
    try {
      const payload = await fetchJson<{ items: Row[]; total: number }>(
        `${ENDPOINT}/dataset?size=200${filters['原始维度名'] ? `&raw_dimension=${encodeURIComponent(filters['原始维度名'])}` : ''}`,
      )
      setDataset(payload.items ?? [])
      setDatasetTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '标准维度数据集读取失败')
    }
  }, [filters])

  const reloadVersions = useCallback(async () => {
    setErrorMessage('')
    try {
      const payload = await fetchJson<{ items: VersionRow[] }>(`${ENDPOINT}/versions`)
      setVersions(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '映射版本读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (tab === 'dataset') void reloadDataset()
    if (tab === 'versions') void reloadVersions()
  }, [tab, reloadDataset, reloadVersions])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '维度映射动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (tab === 'dataset') await reloadDataset()
      if (tab === 'versions') await reloadVersions()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '维度映射操作失败')
    }
  }

  function startEdit(row: Row) {
    setEditingId(Number(row.id))
    setEditValues(Object.fromEntries(
      editableFields.map((field) => [field, String(row[field] ?? '')]),
    ))
  }

  async function saveEdit(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`, {
        method: 'PUT',
        body: JSON.stringify({ values: editValues }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '映射规则保存失败')
      }
      setNotice(payload.message ?? '')
      setEditingId(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '映射规则保存失败')
    }
  }

  async function showTrace(rawDimension: string, rawValue: string, version?: number) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/resolve`, {
        method: 'POST',
        body: JSON.stringify({ values: { 原始维度名: rawDimension, 原始维度值: rawValue, 映射版本: version } }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '解析预览失败')
      }
      setTrace(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '解析预览失败')
    }
  }

  async function rebuild() {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/rebuild`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '重建失败')
      }
      setNotice(`已生成映射版本 ${payload.版本标识}，数据集 ${payload.数据集行数} 行，回算下钻 ${payload.下钻回算行数} 行、聚合 ${payload.聚合回算行数} 行`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重建失败')
    }
  }

  return (
    <section className="page" data-module="dimensionmap">
      <header className="page-head">
        <div>
          <h2>维度映射管理</h2>
          <p className="page-desc">
            按优先级把原始维度转换为标准维度；同一原始维度值只产出一个标准维度映射结果，
            默认映射不会覆盖更具体的映射，命中规则与映射版本可追溯。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void rebuild()}>
            重建映射数据集
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出维度映射清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">映射规则总数</span>
          <strong className="stat-value">{summary["规则总数"] ?? total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已生效规则</span>
          <strong className="stat-value">{summary["已生效规则"] ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前映射版本</span>
          <strong className="stat-value">{summary["当前版本"] ?? '—'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">标准维度行数</span>
          <strong className="stat-value">{summary["数据集行数"] ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">默认命中 / 同级冲突</span>
          <strong className="stat-value">
            {summary["默认命中行数"] ?? 0} / {summary["同级冲突值数"] ?? 0}
          </strong>
        </article>
      </div>

      <nav className="tab-bar">
        <button type="button" className={`tab${tab === 'rules' ? ' active' : ''}`} onClick={() => setTab('rules')}>映射规则</button>
        <button type="button" className={`tab${tab === 'dataset' ? ' active' : ''}`} onClick={() => setTab('dataset')}>标准维度数据集</button>
        <button type="button" className={`tab${tab === 'versions' ? ' active' : ''}`} onClick={() => setTab('versions')}>映射版本</button>
      </nav>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      {tab === 'rules' && (
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((column) => <th key={column}>{column}</th>)}
              <th>状态</th>
              <th>可执行动作</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={String(row.id)}>
                {columns.map((column) => (
                  <td key={column}>
                    {editingId === Number(row.id) && editableFields.includes(column) ? (
                      <input
                        value={editValues[column] ?? ''}
                        onChange={(event) => setEditValues((prev) => ({ ...prev, [column]: event.target.value }))}
                      />
                    ) : (
                      row[column] ?? '—'
                    )}
                  </td>
                ))}
                <td>{String(row.status ?? '—')}</td>
                <td className="row-actions">
                  {editingId === Number(row.id) ? (
                    <>
                      <button className="link" type="button" onClick={() => void saveEdit(row)}>保存</button>
                      <button className="link" type="button" onClick={() => setEditingId(null)}>取消</button>
                    </>
                  ) : (
                    <>
                      <button className="link" type="button" onClick={() => startEdit(row)}>修改</button>
                      {actions.map((action) => (
                        <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                          {action}
                        </button>
                      ))}
                    </>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={columns.length + 2}>暂无维度映射数据，可先登记映射规则</td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {tab === 'dataset' && (
        <table className="data-table">
          <thead>
            <tr>
              <th>原始维度名</th>
              <th>原始维度值</th>
              <th>标准维度名</th>
              <th>标准维度值</th>
              <th>命中规则编号</th>
              <th>规则版本</th>
              <th>映射优先级</th>
              <th>是否默认命中</th>
              <th>映射版本</th>
              <th>来源记录数</th>
              <th>追溯</th>
            </tr>
          </thead>
          <tbody>
            {dataset.map((row) => (
              <tr key={String(row.id)}>
                <td>{String(row["原始维度名"] ?? '—')}</td>
                <td>{String(row["原始维度值"] ?? '—')}</td>
                <td>{String(row["标准维度名"] ?? '—')}</td>
                <td>{String(row["标准维度值"] ?? '—')}</td>
                <td>{String(row["命中规则编号"] ?? '未命中')}</td>
                <td>{String(row["命中规则版本"] ?? '—')}</td>
                <td>{String(row["命中规则优先级"] ?? '—')}</td>
                <td>{row["是否默认命中"] ? '是' : '否'}</td>
                <td>{String(row["映射版本"] ?? '—')}</td>
                <td>{String(row["来源记录数"] ?? 0)}</td>
                <td>
                  <button
                    className="link"
                    type="button"
                    onClick={() => void showTrace(String(row["原始维度名"] ?? ''), String(row["原始维度值"] ?? ''), Number(row["映射版本"]))}
                  >
                    查看候选规则
                  </button>
                </td>
              </tr>
            ))}
            {dataset.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={11}>暂无标准维度数据集，可点击“重建映射数据集”</td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {tab === 'versions' && (
        <table className="data-table">
          <thead>
            <tr>
              <th>版本标识</th>
              <th>状态</th>
              <th>规则数</th>
              <th>生效规则数</th>
              <th>数据集行数</th>
              <th>触发类型</th>
              <th>触发动作</th>
              <th>创建时间</th>
              <th>被替代时间</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((row) => (
              <tr key={row["映射版本"]}>
                <td>{row["版本标识"]}</td>
                <td>{row["状态"]}</td>
                <td>{row["规则数"]}</td>
                <td>{row["生效规则数"]}</td>
                <td>{row["数据集行数"]}</td>
                <td>{row["触发类型"]}</td>
                <td>{row["触发动作"]}</td>
                <td>{row["创建时间"]}</td>
                <td>{row["被替代时间"] ?? '—'}</td>
              </tr>
            ))}
            {versions.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={9}>暂无映射版本</td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {trace && (
        <section className="trace-panel">
          <header>
            <strong>命中追溯</strong>
            <button className="link" type="button" onClick={() => setTrace(null)}>关闭</button>
          </header>
          <p>
            映射版本 {String((trace as Record<string, unknown>)["映射版本"] ?? '—')} ·
            命中来源 {String((trace as Record<string, unknown>)["命中来源"] ?? '—')}
          </p>
          <pre>{JSON.stringify((trace as Record<string, unknown>)["结果"] ?? trace, null, 2)}</pre>
        </section>
      )}

      <footer className="page-foot">
        <span>
          共 {total} 条维度映射记录 · 数据集 {datasetTotal || summary["数据集行数"] || 0} 行
          {summary["冲突处理策略"] ? ` · ${summary["冲突处理策略"]}` : ''}
        </span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
