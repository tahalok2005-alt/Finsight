import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  NavLink, Navigate, Route, BrowserRouter as Router, Routes,
} from "react-router-dom";
import {
  Activity, FileSearch, GitCompareArrows, LayoutDashboard, Search, Upload,
} from "lucide-react";
import clsx from "clsx";
import { OverviewView } from "./views/OverviewView";
import { UploadView } from "./views/UploadView";
import { DocumentView } from "./views/DocumentView";
import { CompareView } from "./views/CompareView";
import { SearchView } from "./views/SearchView";
import { SystemView } from "./views/SystemView";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 15_000,
    },
  },
});

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/upload", label: "Upload", icon: Upload },
  { to: "/search", label: "Search", icon: Search },
  { to: "/compare", label: "Compare", icon: GitCompareArrows },
  { to: "/system", label: "System", icon: Activity },
];

function Shell() {
  return (
    <div className="h-full flex">
      <nav className="w-[200px] shrink-0 bg-ink-900 border-r border-ink-800 flex flex-col">
        <div className="px-4 py-4 border-b border-ink-800">
          <div className="flex items-center gap-2">
            <FileSearch size={18} className="text-accent-400" />
            <span className="font-semibold tracking-tight">FinSight</span>
          </div>
          <p className="text-2xs text-ink-500 mt-1 leading-snug">
            Financial document intelligence
          </p>
        </div>
        <div className="p-2 flex-1 space-y-0.5">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
                  isActive
                    ? "bg-accent-600/15 text-accent-300 font-medium"
                    : "text-ink-300 hover:bg-ink-800 hover:text-ink-100",
                )
              }
            >
              <Icon size={15} />
              {label}
            </NavLink>
          ))}
        </div>
        <div className="p-3 border-t border-ink-800 text-2xs text-ink-500 leading-relaxed">
          Visual-first extraction.
          <br />
          No external LLM API.
        </div>
      </nav>

      <main className="flex-1 min-w-0 overflow-hidden">
        <Routes>
          <Route path="/" element={<OverviewView />} />
          <Route path="/upload" element={<UploadView />} />
          <Route path="/search" element={<SearchView />} />
          <Route path="/compare" element={<CompareView />} />
          <Route path="/system" element={<SystemView />} />
          <Route path="/documents/:id" element={<DocumentView />} />
          <Route path="/documents/:id/:tab" element={<DocumentView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <Shell />
      </Router>
    </QueryClientProvider>
  );
}
