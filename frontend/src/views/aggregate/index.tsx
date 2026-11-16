import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/aggregate'
const columns = [
  "规则编号",
  "源数据粒度",
  "目标粒度",
  "聚合函数集",
  "数据偏移分钟",
  "补全策略",
  "空值处理",
  "输出数据集"
]
const resultColumns = [
  "规则编号",
  "目标维度键",
  "度量值",
  "记录数",
  "输入记录数",
  "覆盖记录数",
  "未覆盖记录数",
  "命中规则编号集合",
  "映射版本"
]
const actions = [
  "提交规则评审",
  "生效规则",
  "废弃规则"
]

type Row = Record<string, string | number | boolean | null>
type Tab = 'rules' | 'results'

type AggregateCheck = {
  "结果分组数": number
  "输入记录数": number
  "覆盖记录数": number
  "未覆盖记录数": number
  "统计记录数合计": number
  "目标键唯一": boolean
  "是否重复": boolean
  "是否丢失": boolean
  "映射版本": number | null
  "未匹配目标维度"?: string[]
}

type MappingLink = {
  "当前版本"?: string | null
  "当前版本映射版本"?: number | null
  rules?: { "规则编号": string; status: string; "核对": AggregateCheck }[]
}

export default function AggregateView() {
  const [tab, setTab] = useState<Tab>('rules')
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [results, setResults] = useState<Row[]>([])
  const [resultTotal, setResultTotal] = useState(0)
  const [link, setLink] = useState<MappingLink>({})
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [resultFilter, setResultFilter] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [list, mapping] = await Promise.all([
        fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?${query}`),
        fetchJson<MappingLink>(`${ENDPOINT}/mapping-link`),
      ])
      setRows(list.items ?? [])
      setTotal(list.total ?? 0)
      setLink(mapping)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置列表读取失败')
    }
  }, [filters])

  const reloadResults = useCallback(async () => {
    setErrorMessage('')
    const query = resultFilter ? `&rule_code=${encodeURIComponent(resultFilter)}` : ''
    try {
      const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}/results?size=200${query}`)
      setResults(payload.items ?? [])
      setResultTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '目标粒度结果读取失败')
    }
  }, [resultFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (tab === 'results') void reloadResults()
  }, [tab, reloadResults])

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
        throw new Error(payload.message ?? '聚合配置动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (tab === 'results') await reloadResults()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合配置操作失败')
    }
  }

  async function recalc(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/recalc`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '目标粒度回算失败')
      }
      setNotice(`已按映射版本 ${payload.映射版本} 回算 ${payload.结果行数} 个分组`)
      await reload()
      await reloadResults()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '目标粒度回算失败')
    }
  }

  const enabledCheck = link.rules?.find((item) => item.status === '已生效')?.['核对']

  return (
    <section className="page" data-module="aggregate">
      <header className="page-head">
        <div>
          <h2>聚合配置管理</h2>
          <p className="page-desc">
            管理数据聚合计算规则；目标粒度结果消费标准维度映射数据集，同一原始维度只映射一次，
            统计不会重复或丢失。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚合配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">聚合规则总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前映射版本</span>
          <strong className="stat-value">{link["当前版本"] ?? '—'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">目标粒度分组数</span>
          <strong className="stat-value">{resultTotal || enabledCheck?.["结果分组数"] || 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">重复 / 丢失</span>
          <strong className="stat-value">
            {enabledCheck ? `${enabledCheck["是否重复"] ? '是' : '否'} / ${enabledCheck["是否丢失"] ? '是' : '否'}` : '—'}
          </strong>
        </article>
      </div>

      <nav className="tab-bar">
        <button type="button" className={`tab${tab === 'rules' ? ' active' : ''}`} onClick={() => setTab('rules')}>聚合规则</button>
        <button type="button" className={`tab${tab === 'results' ? ' active' : ''}`} onClick={() => setTab('results')}>目标粒度结果</button>
      </nav>

      {tab === 'rules' && (
        <>
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
                  {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                  <td>{String(row.status ?? '—')}</td>
                  <td className="row-actions">
                    {actions.map((action) => (
                      <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                        {action}
                      </button>
                    ))}
                    <button className="link" type="button" onClick={() => void recalc(row)}>回算目标粒度</button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={columns.length + 2}>暂无聚合配置数据，可先登记聚合规则</td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}

      {tab === 'results' && (
        <>
          <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reloadResults() }}>
            <label className="filter-item">
              <span>规则编号</span>
              <input
                value={resultFilter}
                placeholder="按规则编号过滤"
                onChange={(event) => setResultFilter(event.target.value)}
              />
            </label>
            <button className="btn" type="submit">查询</button>
            <button className="btn ghost" type="button" onClick={() => setResultFilter('')}>重置条件</button>
          </form>

          <table className="data-table">
            <thead>
              <tr>{resultColumns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {results.map((row) => (
                <tr key={String(row.id)}>
                  {resultColumns.map((column) => (
                    <td key={column}>
                      {column === '命中规则编号集合'
                        ? (Array.isArray(row[column]) ? (row[column] as string[]).join('、') : '—')
                        : row[column] ?? '—'}
                    </td>
                  ))}
                </tr>
              ))}
              {results.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={resultColumns.length}>暂无目标粒度结果，请先生效规则并按当前映射版本回算</td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}

      <footer className="page-foot">
        <span>
          共 {total} 条聚合配置记录 · 结果 {resultTotal || 0} 行
          {enabledCheck ? ` · 统计记录 ${enabledCheck["统计记录数合计"]}/${enabledCheck["覆盖记录数"]} · 未匹配目标维度 ${enabledCheck["未匹配目标维度"]?.join('、') || '无'}` : ''}
        </span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
