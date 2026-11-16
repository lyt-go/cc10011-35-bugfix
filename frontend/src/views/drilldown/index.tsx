import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/drilldown'
const columns = [
  "路径编号",
  "起始维度",
  "下钻层级序列",
  "每层聚合粒度",
  "默认度量",
  "路径深度",
  "缓存秒数",
  "数据新鲜度"
]
const resultColumns = [
  "路径编号",
  "层级序号",
  "原始维度名",
  "标准维度名",
  "标准维度值",
  "度量合计",
  "记录数",
  "命中规则编号集合",
  "映射版本"
]
const actions = [
  "提交路径评审",
  "启用路径",
  "归档路径"
]

type Row = Record<string, string | number | boolean | null>
type Tab = 'paths' | 'results'

type MappingLink = {
  "当前版本"?: string | null
  "当前版本映射版本"?: number | null
  "层级口径"?: string
  paths?: {
    "路径编号": string
    "映射版本": number
    "回算批次": string
    "结果行数": number
    "默认命中行数": number
  }[]
}

export default function DrilldownView() {
  const [tab, setTab] = useState<Tab>('paths')
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
      setErrorMessage(error instanceof Error ? error.message : '多维下钻列表读取失败')
    }
  }, [filters])

  const reloadResults = useCallback(async () => {
    setErrorMessage('')
    const query = resultFilter ? `&path_code=${encodeURIComponent(resultFilter)}` : ''
    try {
      const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}/results?size=200${query}`)
      setResults(payload.items ?? [])
      setResultTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层结果读取失败')
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
        throw new Error(payload.message ?? '多维下钻动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (tab === 'results') await reloadResults()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '多维下钻操作失败')
    }
  }

  async function refreshPath(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/refresh`, { method: 'POST' })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '分层结果刷新失败')
      }
      setNotice(`已按映射版本 ${payload.映射版本} 刷新 ${payload.结果行数} 行分层结果`)
      await reload()
      await reloadResults()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分层结果刷新失败')
    }
  }

  return (
    <section className="page" data-module="drilldown">
      <header className="page-head">
        <div>
          <h2>多维下钻管理</h2>
          <p className="page-desc">
            定义多维度数据下钻的分析路径；分层结果严格消费标准维度映射数据集，
            原始维度名与标准维度名分列展示，映射版本同步更新。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出多维下钻清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">下钻路径总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">当前映射版本</span>
          <strong className="stat-value">{link["当前版本"] ?? '—'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">分层结果行数</span>
          <strong className="stat-value">{resultTotal || link.paths?.reduce((sum, item) => sum + item.结果行数, 0) || 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">默认命中行数</span>
          <strong className="stat-value">{link.paths?.reduce((sum, item) => sum + item.默认命中行数, 0) ?? 0}</strong>
        </article>
      </div>

      <nav className="tab-bar">
        <button type="button" className={`tab${tab === 'paths' ? ' active' : ''}`} onClick={() => setTab('paths')}>下钻路径</button>
        <button type="button" className={`tab${tab === 'results' ? ' active' : ''}`} onClick={() => setTab('results')}>分层结果</button>
      </nav>

      {tab === 'paths' && (
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
                    <button className="link" type="button" onClick={() => void refreshPath(row)}>刷新分层结果</button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={columns.length + 2}>暂无多维下钻数据，可先登记下钻路径</td>
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
              <span>路径编号</span>
              <input
                value={resultFilter}
                placeholder="按路径编号过滤"
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
                  <td className="empty-state" colSpan={resultColumns.length}>暂无分层结果，请先启用路径并按当前映射版本刷新</td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}

      <footer className="page-foot">
        <span>
          共 {total} 条多维下钻记录 · 分层结果 {resultTotal || 0} 行
          {link["层级口径"] ? ` · ${link["层级口径"]}` : ''}
        </span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
