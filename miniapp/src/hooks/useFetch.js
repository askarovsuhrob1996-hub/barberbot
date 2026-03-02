import { useState, useEffect } from 'react'

/**
 * Generic data-fetching hook.
 * @param {() => Promise<any>} fetchFn  – async function that returns data
 * @param {any[]} deps                  – re-fetch when these change (like useEffect deps)
 */
export function useFetch(fetchFn, deps = []) {
  const [data,    setData]    = useState(null)
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchFn()
      .then(d  => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e.message ?? 'Ошибка загрузки') })
      .finally(()=> { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, loading, error }
}
