import { useState, useEffect } from 'react'
import { fetchDates, fetchSlots, fetchServices, createBooking } from '../api'
import { useFetch } from '../hooks/useFetch'

// ── Telegram WebApp helper ────────────────────────────────────────────────────
const tg = window.Telegram?.WebApp
const TG_USER = tg?.initDataUnsafe?.user ?? null

// ── Availability dots ─────────────────────────────────────────────────────────
function AvailabilityDots({ free, total = 8 }) {
  return (
    <div className="flex gap-[3px] justify-center">
      {Array.from({ length: total }).map((_, i) => (
        <span
          key={i}
          className="block h-[5px] w-[5px] rounded-full"
          style={{ backgroundColor: i < free ? '#d4af37' : '#2a2a2a' }}
        />
      ))}
    </div>
  )
}

// ── Time group helpers ────────────────────────────────────────────────────────
function groupSlots(slots) {
  const groups = { '🌅 Утро': [], '☀️ День': [], '🌆 Вечер': [] }
  for (const t of slots) {
    const h = parseInt(t.split(':')[0], 10)
    if      (h < 12) groups['🌅 Утро'].push(t)
    else if (h < 17) groups['☀️ День'].push(t)
    else             groups['🌆 Вечер'].push(t)
  }
  return groups
}

// ── Section wrapper ───────────────────────────────────────────────────────────
function Section({ num, title, children, visible, done }) {
  if (!visible) return null
  return (
    <div className="flex flex-col gap-3 animate-step">
      <div className="flex items-center gap-3">
        <span
          className="flex h-7 w-7 items-center justify-center rounded-full border text-xs font-bold transition-colors duration-300"
          style={{
            borderColor:     done ? '#4ade80' : '#d4af37',
            color:           done ? '#4ade80' : '#d4af37',
            backgroundColor: done ? '#0d2818' : 'transparent',
          }}
        >
          {done ? '✓' : num}
        </span>
        <span className="text-sm font-semibold uppercase tracking-[0.15em] text-[#888888]">
          {title}
        </span>
      </div>
      {children}
      <div className="h-px bg-[#2a2a2a]" />
    </div>
  )
}

// ── Skeleton loaders ──────────────────────────────────────────────────────────
function DateSkeleton() {
  return (
    <div className="flex min-w-[80px] flex-shrink-0 flex-col items-center gap-2 rounded-2xl border border-[#2a2a2a] bg-[#161616] px-3 py-3 animate-pulse">
      <div className="h-3 w-10 rounded bg-[#2a2a2a]" />
      <div className="h-7 w-8 rounded bg-[#2a2a2a]" />
      <div className="h-3 w-8 rounded bg-[#2a2a2a]" />
      <div className="flex gap-[3px]">{Array.from({ length: 8 }).map((_, i) => (
        <span key={i} className="block h-[5px] w-[5px] rounded-full bg-[#2a2a2a]" />
      ))}</div>
    </div>
  )
}

function TimeSkeleton() {
  return (
    <div className="flex flex-wrap gap-2">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="h-9 w-16 rounded-lg bg-[#2a2a2a] animate-pulse" />
      ))}
    </div>
  )
}

function ServiceSkeleton() {
  return (
    <div className="flex items-center justify-between rounded-xl border border-[#2a2a2a] bg-[#161616] p-3 animate-pulse">
      <div className="flex items-center gap-3">
        <div className="h-8 w-8 rounded-full bg-[#2a2a2a]" />
        <div className="flex flex-col gap-1">
          <div className="h-3 w-24 rounded bg-[#2a2a2a]" />
          <div className="h-3 w-16 rounded bg-[#2a2a2a]" />
        </div>
      </div>
      <div className="h-3 w-20 rounded bg-[#2a2a2a]" />
    </div>
  )
}

// ── Contact form ──────────────────────────────────────────────────────────────
function ContactForm({ name, setName, phone, setPhone }) {
  // Pre-fill name from Telegram if available
  useEffect(() => {
    if (TG_USER && !name) {
      const n = [TG_USER.first_name, TG_USER.last_name].filter(Boolean).join(' ')
      if (n) setName(n)
    }
  }, [])

  return (
    <div className="flex flex-col gap-3">
      <input
        type="text"
        placeholder="Ваше имя"
        value={name}
        onChange={e => setName(e.target.value)}
        className="rounded-xl border border-[#2a2a2a] bg-[#161616] px-4 py-3 text-sm text-white placeholder-[#555555] outline-none focus:border-[#d4af37] transition-colors"
      />
      <input
        type="tel"
        placeholder="+998 __ ___ __ __"
        value={phone}
        onChange={e => setPhone(e.target.value)}
        className="rounded-xl border border-[#2a2a2a] bg-[#161616] px-4 py-3 text-sm text-white placeholder-[#555555] outline-none focus:border-[#d4af37] transition-colors"
      />
    </div>
  )
}

function fmt(n) { return Number(n).toLocaleString('ru-RU') }

function serviceIcon(id) {
  if (id.includes('highlight') || id.includes('color')) return '🎨'
  if (id.includes('beard'))   return '🪒'
  if (id.includes('haircut') || id.includes('hair')) return '✂️'
  if (id.includes('styling') || id.includes('style')) return '🧴'
  return '💈'
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Booking() {
  const { data: dates,    loading: datesLoading }    = useFetch(fetchDates)
  const { data: services, loading: servicesLoading } = useFetch(fetchServices)

  const [date,     setDate]     = useState(null)
  const [time,     setTime]     = useState(null)
  const [slots,    setSlots]    = useState(null)
  const [slotsLoading, setSlotsLoading] = useState(false)
  const [selected, setSelected] = useState(new Set())   // selected service ids
  const [name,     setName]     = useState('')
  const [phone,    setPhone]    = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [done,     setDone]     = useState(false)
  const [doneData, setDoneData] = useState(null)
  const [error,    setError]    = useState(null)

  // Fetch time slots when date changes
  useEffect(() => {
    if (!date) return
    setSlots(null)
    setTime(null)
    setSlotsLoading(true)
    fetchSlots(date)
      .then(s  => setSlots(s))
      .catch(() => setSlots([]))
      .finally(() => setSlotsLoading(false))
  }, [date])

  const selectedServices = services?.filter(s => selected.has(s.id)) ?? []
  const totalPrice = selectedServices.reduce((s, x) => s + x.price, 0)
  const totalMins  = selectedServices.reduce((s, x) => s + x.mins,  0)

  function toggleService(id) {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  async function submit() {
    if (!date || !time || selected.size === 0 || !name.trim() || !phone.trim()) return
    setError(null)
    setSubmitting(true)
    try {
      const result = await createBooking({
        user_id:     TG_USER?.id ?? 0,
        date,
        time,
        service_ids: [...selected],
        name:        name.trim(),
        phone:       phone.trim(),
        lang:        'ru',
      })
      setDoneData(result)
      setDone(true)
      tg?.HapticFeedback?.notificationOccurred('success')
    } catch (e) {
      setError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  // ── Success screen ──────────────────────────────────────────────────────────
  if (done) return (
    <div className="flex min-h-[90vh] flex-col items-center justify-center gap-5 px-6 text-center animate-step">
      <div
        className="flex h-20 w-20 items-center justify-center rounded-full border-2 border-[#d4af37] bg-[#1f1a0d]"
        style={{ boxShadow: '0 0 32px #d4af3740' }}
      >
        <span className="text-4xl">⏳</span>
      </div>
      <div className="flex flex-col gap-2">
        <h2 className="text-xl font-bold text-white">Заявка отправлена!</h2>
        <p className="text-sm text-[#888888]">Мастер подтвердит запись в ближайшее время.</p>
      </div>
      <div className="rounded-2xl border border-[#d4af37] bg-[#1f1a0d] px-6 py-3 text-center">
        <p className="text-sm text-[#888888]">Дата и время</p>
        <p className="mt-1 font-bold text-[#d4af37]">
          {date} · {doneData?.time_range ?? time}
        </p>
      </div>
      <button
        onClick={() => {
          setDone(false); setDate(null); setTime(null)
          setSelected(new Set()); setName(''); setPhone(''); setDoneData(null)
        }}
        className="mt-2 rounded-2xl border border-[#2a2a2a] px-8 py-3 text-sm text-[#888888] transition active:scale-95"
      >
        + Новая запись
      </button>
    </div>
  )

  const timeGroups = slots ? groupSlots(slots) : {}

  return (
    <div className="flex flex-col gap-5 px-4 pt-5 pb-36">
      <h2 className="text-lg font-bold text-white">✂️ Новая запись</h2>

      {/* Step 1 — Date */}
      <Section num="1" title="Выберите дату" visible={true} done={!!date}>
        <div className="snap-x-scroll -mx-4 px-4">
          {datesLoading
            ? Array.from({ length: 7 }).map((_, i) => <DateSkeleton key={i} />)
            : dates?.map(d => {
                const selected = date === d.date
                const empty    = d.free === 0
                return (
                  <button
                    key={d.date}
                    disabled={empty}
                    onClick={() => { setDate(d.date); setTime(null) }}
                    className="flex min-w-[80px] flex-shrink-0 flex-col items-center gap-2 rounded-2xl border px-3 py-3 text-center transition active:scale-95"
                    style={{
                      borderColor:     selected ? '#d4af37' : '#2a2a2a',
                      backgroundColor: selected ? '#1f1a0d' : '#161616',
                      opacity:         empty ? 0.35 : 1,
                      cursor:          empty ? 'not-allowed' : 'pointer',
                    }}
                  >
                    <span className="text-xs font-medium uppercase tracking-wider"
                          style={{ color: selected ? '#d4af37' : '#888888' }}>
                      {d.is_today ? 'Сегодня' : d.day}
                    </span>
                    <span className="text-2xl font-bold leading-none"
                          style={{ color: selected ? '#d4af37' : '#ffffff' }}>
                      {d.num}
                    </span>
                    <span className="text-xs" style={{ color: selected ? '#d4af37' : '#888888' }}>
                      {d.month}
                    </span>
                    <AvailabilityDots free={Math.min(d.free, 8)} total={8} />
                  </button>
                )
              })
          }
        </div>
      </Section>

      {/* Step 2 — Time */}
      <Section num="2" title="Выберите время" visible={!!date} done={!!time}>
        {slotsLoading
          ? <TimeSkeleton />
          : slots?.length === 0
            ? <p className="text-sm text-[#888888]">На этот день нет свободных слотов.</p>
            : Object.entries(timeGroups).map(([group, times]) =>
                times.length === 0 ? null : (
                  <div key={group} className="flex flex-col gap-2">
                    <span className="text-xs text-[#888888]">{group}</span>
                    <div className="flex flex-wrap gap-2">
                      {times.map(t => {
                        const sel = time === t
                        return (
                          <button
                            key={t}
                            onClick={() => setTime(t)}
                            className="rounded-lg border px-3 py-2 text-sm font-medium transition active:scale-95"
                            style={{
                              borderColor:     sel ? '#d4af37' : '#2a2a2a',
                              backgroundColor: sel ? '#1f1a0d' : '#161616',
                              color:           sel ? '#d4af37' : '#ffffff',
                            }}
                          >
                            {t}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )
              )
        }
      </Section>

      {/* Step 3 — Services */}
      <Section num="3" title="Выберите услуги" visible={!!time} done={selected.size > 0}>
        <div className="flex flex-col gap-2">
          {servicesLoading
            ? Array.from({ length: 4 }).map((_, i) => <ServiceSkeleton key={i} />)
            : services?.map(s => {
                const sel = selected.has(s.id)
                return (
                  <button
                    key={s.id}
                    onClick={() => toggleService(s.id)}
                    className="flex items-center justify-between rounded-xl border p-3 text-left transition active:scale-95"
                    style={{
                      borderColor:     sel ? '#d4af37' : '#2a2a2a',
                      backgroundColor: sel ? '#1f1a0d' : '#161616',
                    }}
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-xl">{serviceIcon(s.id)}</span>
                      <div>
                        <p className="text-sm font-semibold text-white">{s.name_ru}</p>
                        <p className="text-xs text-[#888888]">{s.mins} мин</p>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className="text-sm font-bold text-[#d4af37]">{fmt(s.price)} сум</span>
                      <div
                        className="flex h-5 w-5 items-center justify-center rounded border text-xs"
                        style={{
                          borderColor:     sel ? '#d4af37' : '#2a2a2a',
                          backgroundColor: sel ? '#d4af37' : 'transparent',
                          color:           '#0d0d0d',
                        }}
                      >
                        {sel && '✓'}
                      </div>
                    </div>
                  </button>
                )
              })
          }
        </div>
      </Section>

      {/* Step 4 — Contact */}
      <Section num="4" title="Ваши данные" visible={selected.size > 0} done={!!name && !!phone}>
        <ContactForm name={name} setName={setName} phone={phone} setPhone={setPhone} />
      </Section>

      {/* Error */}
      {error && (
        <div className="rounded-xl border border-[#ff4444] bg-[#1a0000] px-4 py-3 text-sm text-[#ff6666] animate-step">
          ⚠️ {error}
        </div>
      )}

      {/* Sticky bottom */}
      {time && (
        <div className="fixed bottom-16 left-0 right-0 border-t border-[#2a2a2a] bg-[#0d0d0d]/95 backdrop-blur-sm px-4 py-3 animate-step">
          {selected.size > 0 && (
            <div className="mb-2 flex justify-between text-sm">
              <span className="text-[#888888]">⏱ {totalMins} мин</span>
              <span className="font-bold text-[#d4af37]">{fmt(totalPrice)} сум</span>
            </div>
          )}
          <button
            onClick={submit}
            disabled={selected.size === 0 || !name.trim() || !phone.trim() || submitting}
            className="w-full rounded-2xl py-4 text-sm font-bold uppercase tracking-widest transition-all active:scale-95"
            style={{
              background:  (selected.size > 0 && name && phone && !submitting)
                ? 'linear-gradient(90deg, #d4af37, #f5e070 50%, #d4af37) 0/200%'
                : '#1a1a1a',
              color:       (selected.size > 0 && name && phone && !submitting) ? '#0d0d0d' : '#444444',
              cursor:      (selected.size > 0 && name && phone && !submitting) ? 'pointer' : 'not-allowed',
              boxShadow:   (selected.size > 0 && name && phone && !submitting) ? '0 4px 20px #d4af3740' : 'none',
            }}
          >
            {submitting ? '⏳ Отправка...' : selected.size > 0 && name && phone ? '✅ Записаться' : 'Заполните данные'}
          </button>
        </div>
      )}
    </div>
  )
}
