import { Routes, Route, NavLink, useLocation } from 'react-router-dom'
import { useEffect, useRef } from 'react'
import Home from './pages/Home'
import Booking from './pages/Booking'
import MyBookings from './pages/MyBookings'

function BottomNav() {
  const location = useLocation()

  const links = [
    { to: '/',           icon: '🏠', label: 'Главная' },
    { to: '/booking',    icon: '✂️', label: 'Запись'  },
    { to: '/mybookings', icon: '📋', label: 'Записи'  },
  ]

  return (
    <nav className="fixed bottom-0 left-0 right-0 z-50 flex border-t border-[#2a2a2a] bg-[#0d0d0d]/95 backdrop-blur-sm">
      {links.map(({ to, icon, label }) => {
        const active = location.pathname === to
        return (
          <NavLink
            key={to}
            to={to}
            className="flex flex-1 flex-col items-center pt-3 pb-4 text-xs transition-all duration-200"
            style={{ color: active ? '#d4af37' : '#555555' }}
          >
            <span className="text-xl leading-none" style={{
              filter: active ? 'drop-shadow(0 0 6px #d4af3766)' : 'none',
              transition: 'filter 0.2s',
            }}>
              {icon}
            </span>
            <span className="mt-1 font-medium">{label}</span>
            {active && <span className="nav-dot mt-1" />}
          </NavLink>
        )
      })}
    </nav>
  )
}

function PageWrapper({ children }) {
  const ref = useRef(null)
  const location = useLocation()

  useEffect(() => {
    if (ref.current) {
      ref.current.classList.remove('page-enter')
      void ref.current.offsetWidth // reflow
      ref.current.classList.add('page-enter')
    }
  }, [location.pathname])

  return (
    <div ref={ref} className="page-enter min-h-screen">
      {children}
    </div>
  )
}

export default function App() {
  return (
    <div className="bg-[#0d0d0d] pb-20">
      <PageWrapper>
        <Routes>
          <Route path="/"           element={<Home />} />
          <Route path="/booking"    element={<Booking />} />
          <Route path="/mybookings" element={<MyBookings />} />
        </Routes>
      </PageWrapper>
      <BottomNav />
    </div>
  )
}
