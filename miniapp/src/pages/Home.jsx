import { useNavigate } from 'react-router-dom'
import { fetchServices } from '../api'
import { useFetch } from '../hooks/useFetch'

function serviceIcon(id) {
  if (id.includes('highlight') || id.includes('color')) return '🎨'
  if (id.includes('beard'))   return '🪒'
  if (id.includes('haircut') || id.includes('hair')) return '✂️'
  if (id.includes('styling') || id.includes('style')) return '🧴'
  return '💈'
}

function fmt(n) { return Number(n).toLocaleString('ru-RU') }

function ServiceSkeleton() {
  return (
    <div className="rounded-2xl border border-[#2a2a2a] bg-[#161616] p-4 animate-pulse">
      <div className="h-8 w-8 rounded-full bg-[#2a2a2a]" />
      <div className="mt-2 h-4 w-24 rounded bg-[#2a2a2a]" />
      <div className="mt-1 h-3 w-16 rounded bg-[#2a2a2a]" />
      <div className="mt-2 h-4 w-20 rounded bg-[#2a2a2a]" />
    </div>
  )
}

export default function Home() {
  const navigate = useNavigate()
  const { data: services, loading } = useFetch(fetchServices)

  return (
    <div className="flex flex-col">

      {/* ── Hero ──────────────────────────────────────────────────────────── */}
      <div className="relative w-full overflow-hidden" style={{ height: 260 }}>
        <div className="absolute inset-0 bg-[#161616] flex items-center justify-center">
          <span style={{ fontSize: 120, opacity: 0.15, userSelect: 'none' }}>💈</span>
        </div>
        <div className="absolute inset-0"
          style={{ background: 'linear-gradient(to bottom, #0d0d0d22 0%, #0d0d0d 100%)' }} />
        <div className="absolute top-0 left-0 right-0 h-[2px]"
          style={{ background: 'linear-gradient(90deg, transparent, #d4af37, transparent)' }} />
        <div className="absolute bottom-6 left-5 right-5">
          <p className="text-xs font-semibold uppercase tracking-[0.25em] text-[#d4af37] mb-1">
            Барбершоп
          </p>
          <h1 className="text-3xl font-bold tracking-wide text-white leading-tight">
            ALEX BARBER
          </h1>
          <p className="mt-1 text-sm text-[#888888]">
            📍 Ташкент &nbsp;·&nbsp; Пн–Сб &nbsp;10:00–20:00
          </p>
        </div>
      </div>

      {/* ── Body ──────────────────────────────────────────────────────────── */}
      <div className="flex flex-col gap-5 px-4 pt-5">

        {/* CTA */}
        <button
          onClick={() => navigate('/booking')}
          className="btn-gold w-full rounded-2xl py-4 text-sm shadow-lg transition-transform active:scale-95"
          style={{ boxShadow: '0 4px 24px #d4af3730' }}
        >
          ✂️ Записаться
        </button>

        {/* Divider */}
        <div className="flex items-center gap-3">
          <div className="h-px flex-1 bg-[#2a2a2a]" />
          <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-[#d4af37]">
            Наши услуги
          </span>
          <div className="h-px flex-1 bg-[#2a2a2a]" />
        </div>

        {/* Services grid */}
        <div className="grid grid-cols-2 gap-3">
          {loading
            ? Array.from({ length: 4 }).map((_, i) => <ServiceSkeleton key={i} />)
            : services?.map((s, i) => (
                <div
                  key={s.id}
                  className="rounded-2xl border border-[#2a2a2a] bg-[#161616] p-4 transition-transform active:scale-95"
                  style={{ animationDelay: `${i * 60}ms` }}
                >
                  <span className="text-3xl">{serviceIcon(s.id)}</span>
                  <p className="mt-2 text-sm font-semibold leading-tight text-white">{s.name_ru}</p>
                  <p className="mt-0.5 text-xs text-[#888888]">{s.mins} мин</p>
                  <div className="mt-2 flex items-baseline gap-1">
                    <span className="text-base font-bold text-[#d4af37]">{fmt(s.price)}</span>
                    <span className="text-xs text-[#888888]">сум</span>
                  </div>
                </div>
              ))
          }
        </div>

        {/* My bookings */}
        <button
          onClick={() => navigate('/mybookings')}
          className="flex w-full items-center justify-between rounded-2xl border border-[#2a2a2a] bg-[#161616] px-5 py-4 transition-transform active:scale-95"
        >
          <span className="flex items-center gap-2 text-sm font-medium text-[#888888]">
            <span>📋</span> Мои записи
          </span>
          <span className="text-[#555555] text-lg leading-none">›</span>
        </button>

        <div className="h-2" />
      </div>
    </div>
  )
}
