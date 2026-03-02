const BASE = '/api'

async function req(path) {
  const res = await fetch(BASE + path)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function fetchServices() {
  return req('/services')
}

export async function fetchDates() {
  return req('/dates')
}

export async function fetchSlots(date) {
  const data = await req(`/slots?date=${date}`)
  return data.slots   // string[]
}

export async function fetchBookings(userId) {
  return req(`/bookings?user_id=${userId}`)
}

export async function createBooking(payload) {
  const res = await fetch(`${BASE}/booking`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail ?? `HTTP ${res.status}`)
  return data
}
