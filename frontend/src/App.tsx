import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  NavLink, Navigate, Route, BrowserRouter as Router, Routes,
} from "react-router-dom";
import {
  Activity, FileSearch, Files, GitCompareArrows, LayoutDashboard, Search,
  Upload,
} from "lucide-react";
import clsx from "clsx";
import { DocumentsView } from "./views/DocumentsView";
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

// Two groups, not one list of six.
//
// Three of these are what the work is -- find a document, add one, look across
// them -- and three are tools you reach for occasionally. Presented as one
// flat list they all weighed the same, so the screen asked six questions
// before the user had asked one.
const MAIN: NavItem[] = [
  { to: "/documents", label: "Documents", icon: Files },
  { to: "/upload", label: "Upload", icon: Upload },
  { to: "/overview", label: "Overview", icon: LayoutDashboard },
];

const TOOLS: NavItem[] = [
  { to: "/search", label: "Search", icon: Search },
  { to: "/compare", label: "Compare", icon: GitCompareArrows },
  { to: "/system", label: "System", icon: Activity },
];

type NavItem = { to: string; label: string; icon: typeof Files; end?: boolean };

function NavGroup({ items, title }: { items: NavItem[]; title?: string }) {
  return (
    <div className="space-y-0.5">
      {title && (
        <p className="px-3 pt-4 pb-1.5 text-3xs uppercase tracking-wider text-ink-500 font-semibold">
          {title}
        </p>
      )}
      {items.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            clsx(
              "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
              isActive
                ? "bg-accent-600/10 text-accent-300 font-medium"
                : "text-ink-300 hover:bg-ink-850 hover:text-ink-100",
            )
          }
        >
          <Icon size={16} />
          {label}
        </NavLink>
      ))}
    </div>
  );
}

function Shell() {
  return (
    <div className="h-full flex">
      <nav className="w-[216px] shrink-0 bg-ink-900 border-r border-ink-750 flex flex-col">
        <div className="px-4 py-5 border-b border-ink-750">
          <div className="flex items-center gap-2">
            <FileSearch size={18} className="text-accent-600" />
            <span className="font-semibold tracking-tight text-ink-50">FinSight</span>
          </div>
          <p className="text-2xs text-ink-400 mt-1 leading-snug">
            Financial document intelligence
          </p>
        </div>
        <div className="p-2.5 flex-1">
          <NavGroup items={MAIN} />
          <NavGroup items={TOOLS} title="Tools" />
        </div>
        <div className="p-4 border-t border-ink-750 text-2xs text-ink-400 leading-relaxed">
          Visual-first extraction.
          <br />
          No external LLM API.
        </div>
      </nav>

      <main className="flex-1 min-w-0 overflow-hidden">
        <Routes>
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/overview" element={<OverviewView />} />
          <Route path="/documents" element={<DocumentsView />} />
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
