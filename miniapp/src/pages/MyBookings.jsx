import { useState } from 'react'
import { fetchBookings } from '../api'
import { useFetch } from '../hooks/useFetch'

const tg = window.Telegram?.WebApp
const TG_USER_ID = tg?.initDataUnsafe?.user?.id ?? 0

function fmt(n) { return Number(n).toLocaleString('ru-RU') }

const STATUS = {
  confirmed: { label: 'Подтверждена', icon: '✅', bg: '#0d2818', color: '#4ade80', border: '#1a4a2a' },
  pending:   { label: 'Ожидает',      icon: '⏳', bg: '#1f1700', color: '#fbbf24', border: '#3a2e00' },
}

function CardSkeleton() {
  return (
    <div className="rounded-2xl border border-[#2a2a2a] bg-[#161616] p-4 animate-pulse">
      <div className="flex justify-between">
        <div className="flex flex-col gap-2">
          <div className="h-5 w-24 rounded-full bg-[#2a2a2a]" />
          <div className="h-4 w-32 rounded bg-[#2a2a2a]" />
          <div className="h-3 w-24 rounded bg-[#2a2a2a]" />
        </div>
        <div className="h-5 w-20 rounded bg-[#2a2a2a]" />
      </div>
    </div>
  )
}

function BookingCard({ b }) {
  const [expanded, setExpanded] = useState(false)
  const s = STATUS[b.status] ?? STATUS.pending

  return (
    <div
      className="rounded-2xl border bg-[#161616] overflow-hidden transition-all duration-200"
      style={{ borderColor: expanded ? s.border : '#2a2a2a' }}
    >
      {/* Header */}
      <button
        className="flex w-full items-center justify-between px-4 py-4 text-left"
        onClick={() => setExpanded(v => !v)}
      >
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <span
              className="rounded-full px-2 py-0.5 text-xs font-semibold"
              style={{ backgroundColor: s.bg, color: s.color }}
            >
              {s.icon} {s.label}
            </span>
          </div>
          <span className="text-sm font-semibold text-white mt-1">{b.date_str}</span>
          <span className="text-xs text-[#888888]">🕐 {b.time_str}</span>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className="text-base font-bold text-[#d4af37]">{fmt(b.price)} сум</span>
          <span
            className="text-[#555555] text-lg leading-none transition-transform duration-200"
            style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
          >
            ›
          </span>
        </div>
      </button>

      {/* Expandable details */}
      {expanded && (
        <div className="animate-step border-t border-[#2a2a2a] px-4 py-4 flex flex-col gap-3">
          <div className="flex items-start gap-2 text-sm text-white">
            <span className="mt-0.5">✂️</span>
            <span>{b.services.join(' + ')}</span>
          </div>
          <div className="h-px bg-[#2a2a2a]" />
          <div className="flex gap-2">
            {b.status === 'confirmed' && (
              <button className="flex-1 rounded-xl border border-[#2a2a2a] py-2.5 text-xs font-medium text-[#888888] transition active:scale-95">
                🔄 Перенести
              </button>
            )}
            <button className="flex-1 rounded-xl border border-[#2a2a2a] py-2.5 text-xs font-medium text-[#888888] transition active:scale-95">
              ❌ Отменить
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default function MyBookings() {
  const { data: bookings, loading, error } = useFetch(
    () => fetchBookings(TG_USER_ID),
    [TG_USER_ID]
  )

  if (loading) return (
    <div className="flex flex-col gap-4 px-4 pt-5">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold text-white">Мои записи</h2>
      </div>
      {Array.from({ length: 2 }).map((_, i) => <CardSkeleton key={i} />)}
    </div>
  )

  if (error) return (
    <div className="flex min-h-[80vh] flex-col items-center justify-center gap-4 px-6 text-center">
      <span className="text-4xl" style={{ opacity: 0.4 }}>⚠️</span>
      <p className="text-[#888888] text-sm">Ошибка загрузки записей</p>
      <p className="text-xs text-[#555555]">{error}</p>
    </div>
  )

  if (!bookings?.length) return (
    <div className="flex min-h-[80vh] flex-col items-center justify-center gap-4 px-6 text-center">
      <span className="text-6xl" style={{ opacity: 0.3 }}>📋</span>
      <p className="text-[#888888] text-sm">Нет активных записей</p>
      <p className="text-xs text-[#555555]">Нажмите «Запись» чтобы записаться</p>
    </div>
  )

  return (
    <div className="flex flex-col gap-4 px-4 pt-5">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold text-white">Мои записи</h2>
        <span className="rounded-full bg-[#1f1a0d] border border-[#d4af37] px-2.5 py-0.5 text-xs font-bold text-[#d4af37]">
          {bookings.length}
        </span>
      </div>

      {bookings.map((b, i) => (
        <div key={b.id} className="animate-step" style={{ animationDelay: `${i * 80}ms` }}>
          <BookingCard b={b} />
        </div>
      ))}

      <div className="h-2" />
    </div>
  )
}
