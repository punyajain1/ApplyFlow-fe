"use client";

import { useState, useEffect, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import Papa from "papaparse";
import axios from "axios";

type JobRecord = {
  role_category?: string;
  id?: string | number;
  title?: string;
  company?: string;
  location?: string;
  source?: string;
  description_snippet?: string;
  posted_at?: string;
  scraped_at?: string;
  job_url?: string;
  aiScore?: number;
  [key: string]: any;
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "https://applyflow-fe.onrender.com";

export default function Home() {
  const [loading, setLoading] = useState(false);
  const [scraping, setScraping] = useState(false);
  const [allJobs, setAllJobs] = useState<JobRecord[]>([]);

  const [currentPage, setCurrentPage] = useState(1);
  const JOBS_PER_PAGE = 50;

  const [filters, setFilters] = useState({
    q: "",
    role: "",
    source: "",
  });

  // AI Match States
  const [showAiModal, setShowAiModal] = useState(false);
  const [aiProvider, setAiProvider] = useState<"groq" | "openrouter">("groq");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("llama-3.1-8b-instant");
  const [resumeText, setResumeText] = useState("");
  const [aiLoading, setAiLoading] = useState(false);
  const [aiMode, setAiMode] = useState(false);
  const [aiKeywords, setAiKeywords] = useState<string[]>([]);

  useEffect(() => {
    const savedKey = localStorage.getItem("applyflow_ai_key");
    const savedProvider = localStorage.getItem("applyflow_ai_provider");
    if (savedKey) setAiKey(savedKey);
    if (savedProvider) setAiProvider(savedProvider as any);
  }, []);

  const saveAiSettings = (provider: string, key: string) => {
    localStorage.setItem("applyflow_ai_provider", provider);
    localStorage.setItem("applyflow_ai_key", key);
  };

  useEffect(() => {
    fetchJobs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fetchJobs = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/jobs`);
      if (res.data && res.data.success) {
        setAllJobs(res.data.jobs || []);
        setAiMode(false);
        setAiKeywords([]); // Reset keywords on fresh fetch
      }
    } catch (err) {
      console.error("Failed to fetch jobs:", err);
    } finally {
      setLoading(false);
    }
  };

  const triggerScrape = async () => {
    setScraping(true);
    try {
      // Step 1: Kick off the background job — returns immediately with a job_id
      const startRes = await axios.get(`${API_BASE}/scrape-everything`);
      if (startRes.data?.status === 429 || startRes.status === 429) {
        alert("Daily limit reached (5/5). Please try again tomorrow.");
        return;
      }

      const jobId = startRes.data?.job_id;
      if (!jobId) throw new Error("No job_id returned from server");

      // Step 2: Poll /scrape-status/<job_id> every 5 seconds until done or error
      await new Promise<void>((resolve, reject) => {
        const interval = setInterval(async () => {
          try {
            const statusRes = await axios.get(`${API_BASE}/scrape-status/${jobId}`);
            const { status } = statusRes.data;
            if (status === "done") {
              clearInterval(interval);
              resolve();
            } else if (status === "error") {
              clearInterval(interval);
              reject(new Error(statusRes.data?.error || "Scrape job failed"));
            }
            // else still "running" — keep polling
          } catch (pollErr) {
            clearInterval(interval);
            reject(pollErr);
          }
        }, 5000);
      });

      // Step 3: Fetch updated jobs from the sheet
      await fetchJobs();
    } catch (err: any) {
      console.error("Scraping failed:", err);
      if (err.response?.status === 429) {
        alert("Daily limit reached (5/5). Please try again tomorrow.");
      } else {
        alert("Failed to trigger scrape: " + (err.message || "Unknown error"));
      }
    } finally {
      setScraping(false);
    }
  };


  const runAIMatch = async () => {
    if (!aiKey || !resumeText) {
      alert("Please provide both an API key and your resume text.");
      return;
    }
    setAiLoading(true);
    saveAiSettings(aiProvider, aiKey);

    const prompt = `You are an expert technical recruiter. Analyze the following resume text and extract all highly relevant keywords that should be matched against job descriptions (up to 30 keywords).
Include:
1. Technical skills and tools.
2. The specific job titles the candidate qualifies for.
3. The candidate's experience level and target role keywords (e.g., "fresher", "graduate", "entry level", "intern", "junior", "0 years experience") based on their background.

Respond ONLY with a valid JSON array of strings. No markdown formatting, no explanations, just the raw JSON array. Example: ["React", "Python", "Software Engineer", "Fresher", "Graduate", "Entry Level"]
Resume:
${resumeText}`;

    const endpoint =
      aiProvider === "groq"
        ? "https://api.groq.com/openai/v1/chat/completions"
        : "https://openrouter.ai/api/v1/chat/completions";

    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${aiKey}`,
        "Content-Type": "application/json",
      };

      if (aiProvider === "openrouter") {
        headers["HTTP-Referer"] = window.location.href;
        headers["X-Title"] = "ApplyFlow Dashboard";
      }

      const response = await axios.post(
        endpoint,
        {
          model: aiModel,
          messages: [{ role: "user", content: prompt }],
          temperature: 0.1,
        },
        { headers }
      );

      let content = response.data.choices[0].message.content.trim();
      // Clean up markdown code blocks if the LLM hallucinated them
      if (content.startsWith("```json")) content = content.substring(7);
      if (content.startsWith("```")) content = content.substring(3);
      if (content.endsWith("```")) content = content.slice(0, -3);

      const keywords: string[] = JSON.parse(content);

      if (!Array.isArray(keywords)) throw new Error("LLM did not return an array");

      // Score jobs
      const scoredJobs = allJobs.map((job) => {
        let score = 0;
        const titleLower = (job.title || "").toLowerCase();
        const descLower = (job.description_snippet || "").toLowerCase();
        const roleLower = (job.role_category || "").toLowerCase();

        keywords.forEach((kw) => {
          const kwLower = kw.toLowerCase();
          if (titleLower.includes(kwLower)) {
            score += 2; // High weight for title match
          } else if (descLower.includes(kwLower) || roleLower.includes(kwLower)) {
            score += 1; // Standard weight for description or category match
          }
        });

        return { ...job, aiScore: score };
      });

      setAllJobs(scoredJobs);
      setAiKeywords(keywords);
      setAiMode(true);
      setCurrentPage(1);
      setShowAiModal(false);
    } catch (err: any) {
      console.error("AI Match Failed:", err);
      const errorMessage = err.response?.data?.error?.message || err.message;
      alert(`API Error: ${errorMessage}\n\nPlease check your API key and ensure the selected model supports your resume length.`);
    } finally {
      setAiLoading(false);
    }
  };

  const clearAiMatch = () => {
    setAiMode(false);
    setAiKeywords([]);
    setCurrentPage(1);
  };

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    setFilters({ ...filters, [e.target.name]: e.target.value });
    setCurrentPage(1);
  };

  const filteredJobs = useMemo(() => {
    let result = allJobs.filter((job) => {
      const matchQ =
        filters.q === "" ||
        job.title?.toLowerCase().includes(filters.q.toLowerCase()) ||
        job.company?.toLowerCase().includes(filters.q.toLowerCase()) ||
        job.description_snippet?.toLowerCase().includes(filters.q.toLowerCase());

      const matchRole =
        filters.role === "" ||
        job.role_category?.toLowerCase().includes(filters.role.toLowerCase());

      const matchSource =
        filters.source === "" ||
        (filters.source === "hn"
          ? job.source?.toLowerCase() === "hn"
          : job.source?.toLowerCase().includes(filters.source.toLowerCase()));

      return matchQ && matchRole && matchSource;
    });

    if (aiMode) {
      // Sort descending by aiScore
      result = result.sort((a, b) => (b.aiScore || 0) - (a.aiScore || 0));
      // Only keep jobs with score > 0 for relevance, unless results are too small
      result = result.filter(job => (job.aiScore || 0) > 0);
    }

    return result;
  }, [allJobs, filters, aiMode]);

  const totalPages = Math.ceil(filteredJobs.length / JOBS_PER_PAGE);
  const paginatedJobs = filteredJobs.slice(
    (currentPage - 1) * JOBS_PER_PAGE,
    currentPage * JOBS_PER_PAGE
  );

  const downloadCSV = () => {
    if (filteredJobs.length === 0) return;
    const csv = Papa.unparse(filteredJobs);
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", aiMode ? "ai_matched_jobs.csv" : "jobs_export.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return "";
    try {
      const d = new Date(dateString);
      return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    } catch {
      return dateString.slice(0, 10);
    }
  };

  const lastSyncDate = useMemo(() => {
    if (!allJobs || allJobs.length === 0) return null;
    const lastJob = allJobs[allJobs.length - 1]; // Get date from the last row
    const dateStr = lastJob.scraped_at || lastJob.posted_at;
    return dateStr ? new Date(dateStr) : null;
  }, [allJobs]);

  const formatTimeAgo = (date: Date) => {
    const diffMs = Date.now() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHrs = Math.floor(diffMins / 60);
    if (diffHrs < 24) return `${diffHrs}h ago`;
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  };

  return (
    <main className="min-h-screen bg-[#050505] text-neutral-200 font-sans selection:bg-white selection:text-black pb-32">

      {/* AI Match Modal */}
      <AnimatePresence>
        {showAiModal && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm"
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="w-full max-w-2xl bg-[#0a0a0a] border border-white/10 rounded-3xl p-8 flex flex-col shadow-2xl"
            >
              <div className="flex justify-between items-center mb-8">
                <h2 className="text-2xl font-medium text-white tracking-tight">AI Resume Matcher</h2>
                <button
                  onClick={() => setShowAiModal(false)}
                  className="text-neutral-500 hover:text-white transition-colors uppercase tracking-widest text-xs font-semibold"
                >
                  Close
                </button>
              </div>

              <div className="space-y-6">
                <div className="flex flex-col gap-2">
                  <label className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">AI Provider</label>
                  <div className="flex gap-4">
                    <button
                      onClick={() => {
                        setAiProvider("groq");
                        setAiModel("llama-3.1-8b-instant");
                      }}
                      className={`flex-1 py-3 rounded-xl border ${aiProvider === "groq" ? "bg-white text-black border-white" : "bg-transparent text-neutral-400 border-white/10 hover:border-white/30"} transition-all text-sm font-semibold`}
                    >
                      Groq (Llama 3.1 8B)
                    </button>
                    <button
                      onClick={() => {
                        setAiProvider("openrouter");
                        setAiModel("meta-llama/llama-3.1-8b-instruct:free");
                      }}
                      className={`flex-1 py-3 rounded-xl border ${aiProvider === "openrouter" ? "bg-white text-black border-white" : "bg-transparent text-neutral-400 border-white/10 hover:border-white/30"} transition-all text-sm font-semibold`}
                    >
                      OpenRouter (Free)
                    </button>
                  </div>
                </div>

                <div className="flex flex-col gap-2">
                  <label className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">Model ID</label>
                  <input
                    type="text"
                    value={aiModel}
                    onChange={(e) => setAiModel(e.target.value)}
                    placeholder="e.g. meta-llama/llama-3.1-8b-instruct:free"
                    className="w-full bg-[#111] border border-white/10 rounded-xl px-4 py-3 text-white placeholder-neutral-600 focus:outline-none focus:border-white/30 transition-all text-[14px]"
                  />
                </div>

                <div className="flex flex-col gap-2">
                  <div className="flex justify-between items-center">
                    <label className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">API Key</label>
                    <a
                      href={aiProvider === "groq" ? "https://console.groq.com/keys" : "https://openrouter.ai/settings/keys"}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[10px] text-neutral-400 hover:text-white transition-colors uppercase tracking-widest font-bold flex items-center gap-1"
                    >
                      Get Key ↗
                    </a>
                  </div>
                  <input
                    type="password"
                    value={aiKey}
                    onChange={(e) => setAiKey(e.target.value)}
                    placeholder={`Enter your ${aiProvider === "groq" ? "Groq" : "OpenRouter"} API Key`}
                    className="w-full bg-[#111] border border-white/10 rounded-xl px-4 py-3.5 text-white placeholder-neutral-600 focus:outline-none focus:border-white/30 transition-all text-[14px]"
                  />
                  <span className="text-[11px] text-neutral-600">Your key is only stored locally in your browser.</span>
                </div>

                <div className="flex flex-col gap-2">
                  <label className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">Resume Text</label>
                  <textarea
                    value={resumeText}
                    onChange={(e) => setResumeText(e.target.value)}
                    placeholder="Paste your entire resume here..."
                    className="w-full bg-[#111] border border-white/10 rounded-xl px-4 py-3.5 text-white placeholder-neutral-600 focus:outline-none focus:border-white/30 transition-all text-[14px] min-h-[160px] resize-none"
                  />
                </div>

                <button
                  onClick={runAIMatch}
                  disabled={aiLoading}
                  className="w-full py-4 rounded-xl bg-white text-black hover:bg-neutral-200 transition-colors font-semibold disabled:opacity-50 mt-4"
                >
                  {aiLoading ? "Analyzing Resume & Scoring Jobs..." : "Find Best Matches"}
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Header */}
      <header className="sticky top-0 z-50 bg-[#050505]/70 backdrop-blur-2xl border-b border-white/5 px-4 sm:px-8 py-4">
        <div className="w-full max-w-[96%] mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="flex items-baseline gap-4">
            <h1 className="text-2xl font-semibold tracking-tighter text-white">
              ApplyFlow
            </h1>
            {lastSyncDate && (
              <span className="text-[13px] text-neutral-500 font-medium tracking-wide">
                Synced {formatTimeAgo(lastSyncDate)}
              </span>
            )}
          </div>

          <div className="flex items-center gap-4 text-sm font-medium w-full md:w-auto">
            <button
              onClick={triggerScrape}
              disabled={scraping}
              className="flex-1 md:flex-none px-6 py-2.5 rounded-full bg-white/5 hover:bg-white/10 border border-white/5 transition-colors text-white disabled:opacity-50"
            >
              {scraping ? "Syncing Data..." : "Force Sync"}
            </button>

            <button
              onClick={fetchJobs}
              disabled={loading}
              className="flex-1 md:flex-none px-6 py-2.5 rounded-full bg-white text-black hover:bg-neutral-200 transition-colors disabled:opacity-50"
            >
              {loading ? "Loading..." : "Refresh"}
            </button>
          </div>
        </div>
      </header>

      <div className="w-full max-w-[96%] mx-auto px-4 sm:px-8 mt-10">

        {/* Controls Section */}
        <div className="flex flex-col lg:flex-row gap-4 mb-10">
          <input
            name="q"
            value={filters.q}
            onChange={handleChange}
            placeholder="Search keywords, titles, or companies"
            className="flex-1 bg-[#0a0a0a] border border-white/10 rounded-2xl px-6 py-4 text-white placeholder-neutral-600 focus:outline-none focus:border-white/30 transition-all text-[15px]"
          />

          <div className="flex flex-col sm:flex-row gap-4">
            <select
              name="role"
              value={filters.role}
              onChange={handleChange}
              className="bg-[#0a0a0a] border border-white/10 rounded-2xl px-6 py-4 text-neutral-300 focus:outline-none focus:border-white/30 transition-all text-[14px] appearance-none cursor-pointer min-w-[160px]"
            >
              <option value="">All Roles</option>
              <option value="sde">SDE / SWE</option>
              <option value="full stack">Full Stack</option>
              <option value="backend">Backend</option>
              <option value="frontend">Frontend</option>
              <option value="genai">GenAI / AI</option>
              <option value="yc">YC / HN</option>
            </select>

            <select
              name="source"
              value={filters.source}
              onChange={handleChange}
              className="bg-[#0a0a0a] border border-white/10 rounded-2xl px-6 py-4 text-neutral-300 focus:outline-none focus:border-white/30 transition-all text-[14px] appearance-none cursor-pointer min-w-[160px]"
            >
              <option value="">All Sources</option>
              <option value="linkedin">LinkedIn</option>
              <option value="indeed">Indeed</option>
              <option value="hn_jobs">HN Jobs</option>
              <option value="hn">HN Posts</option>
            </select>

            <button
              onClick={() => setShowAiModal(true)}
              className="px-6 py-2.5 rounded-full bg-white text-black text-sm font-semibold hover:bg-neutral-200 transition-colors flex items-center gap-2 shadow-[0_0_15px_rgba(255,255,255,0.2)]"
            >
              AI Match
              <span className="bg-black/10 text-black text-[9px] font-bold px-1.5 py-0.5 rounded-md uppercase tracking-wider -mr-1">BETA</span>
            </button>

            <button
              onClick={downloadCSV}
              className="px-6 py-4 rounded-2xl bg-[#0a0a0a] hover:bg-[#111] border border-white/10 text-neutral-300 transition-colors text-[14px] font-medium"
            >
              Export
            </button>
          </div>
        </div>

        {/* Info Bar */}
        <div className="mb-8 flex flex-col gap-4">
          <div className="flex justify-between items-center flex-wrap gap-4 border-b border-white/5 pb-4">
            <div className="flex items-center gap-4">
              <span className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">
                {aiMode ? "AI Matches Found" : "Found"}{" "}
                <span className="text-white mx-1">{filteredJobs.length}</span> Opportunities
              </span>
              {aiMode && (
                <button
                  onClick={clearAiMatch}
                  className="px-3 py-1 rounded-full bg-white/10 hover:bg-white/20 text-white text-[10px] font-bold uppercase tracking-widest transition-colors"
                >
                  Clear AI Filter
                </button>
              )}
            </div>

            {totalPages > 1 && (
              <span className="text-[12px] font-semibold tracking-widest text-neutral-500 uppercase">
                Page <span className="text-white">{currentPage}</span> of {totalPages}
              </span>
            )}
          </div>

          {aiMode && aiKeywords.length > 0 && (
            <div className="bg-white/5 border border-white/10 rounded-2xl p-5 flex flex-col gap-3 max-h-40 overflow-y-auto">
              <span className="text-[11px] font-semibold tracking-widest text-neutral-400 uppercase">
                Scoring Context: +2 points for title match, +1 point for description match
              </span>
              <div className="flex flex-wrap gap-2">
                {aiKeywords.map((kw, i) => (
                  <span key={i} className="px-3 py-1.5 bg-[#0a0a0a] border border-white/10 rounded-full text-[12px] font-medium text-white shadow-sm">
                    {kw}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Job Grid */}
        {loading && allJobs.length === 0 ? (
          <div className="py-32 flex flex-col items-center justify-center text-neutral-500">
            <span className="text-sm font-medium tracking-wider uppercase">Loading jobs...</span>
          </div>
        ) : filteredJobs.length === 0 ? (
          <div className="py-32 flex flex-col items-center justify-center text-neutral-600 bg-[#0a0a0a] rounded-3xl border border-white/5">
            <span className="text-lg font-medium text-white mb-2">No results</span>
            <span className="text-sm">Try broadening your search criteria.</span>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
            <AnimatePresence>
              {paginatedJobs.map((job, idx) => {
                const isHNPost = job.source?.toLowerCase() === "hn";

                return (
                  <motion.div
                    layout
                    initial={{ opacity: 0, y: 15 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ duration: 0.3, ease: [0.23, 1, 0.32, 1] }}
                    key={job.id || idx}
                    className="group bg-[#0a0a0a] rounded-[16px] p-5 border border-white/5 hover:border-white/20 hover:bg-[#0f0f0f] transition-all duration-300 flex flex-col relative overflow-hidden"
                  >
                    {aiMode && job.aiScore !== undefined && (
                      <div className="absolute top-0 right-0 bg-white/10 text-white text-[9px] font-bold px-3 py-1 rounded-bl-xl uppercase tracking-widest z-10">
                        Score: {job.aiScore}
                      </div>
                    )}

                    {isHNPost ? (
                      <>
                        <div className="mb-4 mt-2">
                          <div className="flex items-center gap-2 mb-2">
                            <div className="w-5 h-5 rounded-full bg-white/10 flex items-center justify-center text-neutral-300 text-[10px] font-bold">
                              {(job.company || job.by || "U")[0].toUpperCase()}
                            </div>
                            <h3 className="text-[14px] font-medium text-white leading-snug line-clamp-1">
                              {job.company || job.by || "Unknown User"}
                            </h3>
                          </div>
                          <div className="text-[12px] text-neutral-500 font-medium">
                            Community Post
                          </div>
                        </div>

                        {job.description_snippet && (
                          <p className="text-[13px] text-neutral-400 line-clamp-4 leading-relaxed mb-6 flex-grow">
                            {job.description_snippet}
                          </p>
                        )}

                        <div className="mt-auto pt-4 border-t border-white/5 flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <span className="px-2 py-1 text-[9px] font-semibold uppercase tracking-widest rounded-full border border-white/10 text-neutral-500">
                              HN Post
                            </span>
                            <span className="text-[10px] text-neutral-500 font-semibold tracking-widest uppercase ml-1">
                              {formatDate(job.posted_at || job.scraped_at)}
                            </span>
                          </div>
                          {job.job_url && (
                            <a
                              href={job.job_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="px-3 py-1.5 rounded-full bg-white text-black hover:bg-neutral-200 text-[10px] font-bold uppercase tracking-wider opacity-0 group-hover:opacity-100 transition-all duration-300 translate-y-1 group-hover:translate-y-0"
                            >
                              View Post
                            </a>
                          )}
                        </div>
                      </>
                    ) : (
                      <>
                        <div className="mb-4 mt-2">
                          <h3 className="text-[15px] font-medium text-white leading-snug mb-2 line-clamp-2">
                            {job.title || "Untitled"}
                          </h3>
                          <div className="text-[13px] text-neutral-400 font-medium">
                            {job.company || job.by || "Unknown Company"}
                          </div>
                        </div>

                        <div className="flex flex-wrap gap-2 mb-4">
                          {job.role_category && (
                            <span className="px-2 py-1 text-[9px] font-semibold uppercase tracking-widest rounded-full bg-white/5 text-neutral-300">
                              {job.role_category}
                            </span>
                          )}
                          {job.source && (
                            <span className="px-2 py-1 text-[9px] font-semibold uppercase tracking-widest rounded-full border border-white/10 text-neutral-500">
                              {job.source}
                            </span>
                          )}
                        </div>

                        {job.location && (
                          <div className="text-[11px] text-neutral-500 mb-4 font-medium line-clamp-1">
                            {job.location}
                          </div>
                        )}

                        {job.description_snippet && (
                          <p className="text-[12px] text-neutral-400 line-clamp-3 leading-relaxed mb-6 flex-grow">
                            {job.description_snippet}
                          </p>
                        )}

                        <div className="mt-auto pt-4 border-t border-white/5 flex items-center justify-between">
                          <div className="text-[10px] text-neutral-500 font-semibold tracking-widest uppercase">
                            {formatDate(job.posted_at || job.scraped_at)}
                          </div>
                          {job.job_url && (
                            <a
                              href={job.job_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="px-3 py-1.5 rounded-full bg-white text-black hover:bg-neutral-200 text-[10px] font-bold uppercase tracking-wider opacity-0 group-hover:opacity-100 transition-all duration-300 translate-y-1 group-hover:translate-y-0"
                            >
                              Apply
                            </a>
                          )}
                        </div>
                      </>
                    )}
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>
        )}

        {/* Pagination Controls */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between mt-12 pt-6 border-t border-white/5">
            <button
              onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
              disabled={currentPage === 1}
              className="px-6 py-2.5 rounded-full border border-white/10 text-neutral-400 hover:text-white hover:border-white/30 transition-colors disabled:opacity-30 disabled:hover:text-neutral-400 disabled:hover:border-white/10 text-[13px] uppercase tracking-widest font-semibold"
            >
              Previous
            </button>
            <button
              onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
              disabled={currentPage === totalPages}
              className="px-6 py-2.5 rounded-full border border-white/10 text-neutral-400 hover:text-white hover:border-white/30 transition-colors disabled:opacity-30 disabled:hover:text-neutral-400 disabled:hover:border-white/10 text-[13px] uppercase tracking-widest font-semibold"
            >
              Next
            </button>
          </div>
        )}
      </div>
    </main>
  );
}
