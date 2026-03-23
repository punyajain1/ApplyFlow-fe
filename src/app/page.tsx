"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, Loader2, Download, SlidersHorizontal, ArrowRight, Github } from "lucide-react";
import Papa from "papaparse";

export default function Home() {
  const [loading, setLoading] = useState(false);
  const [jobs, setJobs] = useState<any[]>([]);
  
  const [formData, setFormData] = useState({
    searchTerm: "",
    location: "",
    googleSearchTerm: "",
    hoursOld: "",
    defaultCountry: "",
    jobType: "",
    isRemote: "false",
    internshalaSearchTerm: "",
  });

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    let value = e.target.value;
    // Prevent negative numbers for hoursOld
    if (e.target.name === 'hoursOld') {
      if (Number(value) < 0) value = '0';
    }
    setFormData({ ...formData, [e.target.name]: value });
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setJobs([]);
    
    try {
      // Map frontend camelCase to backend UPPER_CASE params
      const apiParams: Record<string, any> = {
        SEARCH_TERM: formData.searchTerm,
        LOCATION: formData.location,
        GOOGLE_SEARCH_TERM: formData.googleSearchTerm,
        HOURS_OLD: Number(formData.hoursOld) || 72,
        DEFAULT_COUNTRY: formData.defaultCountry,
        JOB_TYPE: formData.jobType,
        IS_REMOTE: formData.isRemote === "true",
        INTERNSHALA_SEARCH_TERM: formData.internshalaSearchTerm,
      };

      // Filter out empty string params if needed
      const filteredParams = Object.fromEntries(
        Object.entries(apiParams).filter(([_, v]) => v !== "" && v !== null && v !== undefined)
      );

      // Sending directly to Railway from the browser to bypass Vercel's 60-second limit
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "https://jobscrappertelegrambot-production.up.railway.app/job-search";

      const res = await fetch(apiUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(filteredParams)
      });
      
      const data = await res.json();

      if (!res.ok || data.success === false) {
        throw new Error(data.message || "Failed to fetch jobs from server");
      }
      
      setJobs(data.jobs || []);
      setLoading(false);
      
    } catch (err: any) {
      console.error("Failed to fetch jobs:", err);
      alert(`Error fetching jobs: ${err.message || 'Check console for details.'}`);
      setLoading(false);
    }
  };

  const getOrderedColumns = () => {
    if (jobs.length === 0) return [];
    const allKeys = Array.from(new Set(jobs.flatMap((job) => Object.keys(job))));
    const primaryKeys = [
      "id",
      "site",
      "title",
      "job_type",
      "location",
      "company",
      "link",
      "job_url",
      "job_function",
      "job description",
      "job_description",
      "description"
    ];
    const existingPrimaryKeys = primaryKeys.filter((key) => allKeys.includes(key));
    const remainingKeys = allKeys.filter((key) => !primaryKeys.includes(key));
    return [...existingPrimaryKeys, ...remainingKeys];
  };

  const downloadCSV = () => {
    if (jobs.length === 0) return;

    const orderedColumns = getOrderedColumns();

    const csv = Papa.unparse(jobs, {
      columns: orderedColumns,
    });
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", "jobs_export.csv");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const setPreset = (preset: "bcom_intern" | "software_developer" | "software_intern") => {
    if (preset === "bcom_intern") setFormData(f => ({ ...f, searchTerm: "finance intern", googleSearchTerm: "commerce graduate fresher jobs in India", internshalaSearchTerm: "accounting-finance", jobType: "", isRemote: "false", hoursOld: "120", defaultCountry: "India", location: "India" }));
    if (preset === "software_developer") setFormData(f => ({ ...f, searchTerm: "software developer", googleSearchTerm: "software developer jobs in India", internshalaSearchTerm: "software-development", jobType: "", isRemote: "false", hoursOld: "120", defaultCountry: "India", location: "India" }));
    if (preset === "software_intern") setFormData(f => ({ ...f, searchTerm: "software engineer intern", googleSearchTerm: "software engineer intern jobs in India", internshalaSearchTerm: "software-engineering", jobType: "", isRemote: "false", hoursOld: "120", defaultCountry: "India", location: "India" }));
  };

  return (
    <main className="min-h-screen flex flex-col items-center pt-24 pb-12 px-4 selection:bg-neutral-800 selection:text-white">
      
      {/* Top Pill */}
      <motion.div 
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center gap-2 px-4 py-1.5 rounded-full border border-neutral-200/80 bg-white/50 backdrop-blur-sm text-[13px] font-medium text-neutral-500 tracking-wide mb-14"
      >
        <div className="w-2 h-2 rounded-full bg-neutral-800" /> Free forever · No sign up
      </motion.div>

      {/* Main Typography */}
      <motion.div 
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1 }}
        className="text-center space-y-4 mb-8"
      >
        <h1 className="text-6xl md:text-7xl font-medium tracking-tight text-neutral-900 leading-tight">
          Find opportunities
          <br />
          <span className="font-playfair italic font-normal text-neutral-800">worth applying to</span>
        </h1>
        <p className="text-neutral-500 text-lg max-w-105 mx-auto leading-relaxed pt-2">
          Drop any role, location, or parameter. <br />
          Beautifully formatted CSVs in seconds.
        </p>
      </motion.div>

      {/* Interactive Form Card */}
      <motion.div 
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ delay: 0.2 }}
        className="max-w-175 w-full"
      >
        <div className="bg-white rounded-4xl p-8 md:p-10 shadow-[0_8px_30px_rgb(0,0,0,0.04)] border border-neutral-100 relative overflow-hidden group">
          
          <form onSubmit={handleSearch} className="relative z-10 flex flex-col gap-6">
            
            {/* Minimal High-impact Inputs */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                <label className="text-[13px] font-medium ml-1">Search Term <span className="text-red-500">*</span></label>
                <input 
                  required
                  name="searchTerm" value={formData.searchTerm} onChange={handleChange}
                  placeholder="e.g. software developer"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3.5 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all font-medium text-[15px]"
                />
              </div>
              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                <label className="text-[13px] font-medium ml-1">Location <span className="text-red-500">*</span></label>
                <input 
                  required
                  name="location" value={formData.location} onChange={handleChange}
                  placeholder="e.g. India"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3.5 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all font-medium text-[15px]"
                />
              </div>
            </div>

            {/* Advanced Filters (Always Visible) */}
            <div className="pt-2 grid grid-cols-1 md:grid-cols-2 gap-4 border-t border-neutral-100 mt-2">
              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                  <label className="text-[12px] font-medium ml-1">Job Type</label>
                  <select 
                  name="jobType" value={formData.jobType} onChange={handleChange}
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-800 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm appearance-none"
                >
                  <option value="">Any</option>
                  <option value="fulltime">Full-time</option>
                  <option value="parttime">Part-time</option>
                  <option value="internship">Internship</option>
                  <option value="contract">Contract</option>
                </select>
              </div>
              
              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                <label className="text-[12px] font-medium ml-1">Hours Old (Max) <span className="text-red-500">*</span></label>
                <input 
                  required
                  type="number" name="hoursOld" value={formData.hoursOld} onChange={handleChange}
                  placeholder="e.g. 72"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm"
                />
              </div>

              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                <label className="text-[12px] font-medium ml-1">Default Country <span className="text-red-500">*</span></label>
                <input 
                  required
                  name="defaultCountry" value={formData.defaultCountry} onChange={handleChange}
                  placeholder="e.g. India"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm"
                />
              </div>

              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500">
                <label className="text-[12px] font-medium ml-1">Is Remote <span className="text-red-500">*</span></label>
                <select 
                  required
                  name="isRemote" value={formData.isRemote} onChange={handleChange}
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-800 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm appearance-none"
                >
                  <option value="true">Yes</option>
                  <option value="false">No</option>
                </select>
              </div>

              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500 md:col-span-2">
                <label className="text-[12px] font-medium ml-1">Internshala Search Term <span className="text-red-500">*</span></label>
                <input 
                  required
                  name="internshalaSearchTerm" value={formData.internshalaSearchTerm} onChange={handleChange}
                  placeholder="e.g. software-development"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm"
                />
              </div>

              <div className="flex flex-col gap-1.5 focus-within:text-black text-neutral-500 md:col-span-2">
                <label className="text-[12px] font-medium ml-1">Google Search Exact Syntax <span className="text-red-500">*</span></label>
                <input 
                  required
                  name="googleSearchTerm" value={formData.googleSearchTerm} onChange={handleChange}
                  placeholder="e.g. software developer jobs in India"
                  className="w-full bg-neutral-50/50 border border-neutral-200/80 rounded-2xl px-4 py-3 text-neutral-900 placeholder-neutral-400 focus:outline-none focus:ring-2 focus:ring-neutral-900/10 focus:border-neutral-300 transition-all text-sm"
                />
              </div>
            </div>

            {/* Action State */}
            <div className="flex justify-center mt-4">
              <button 
                type="submit" 
                disabled={loading}
                className="group relative inline-flex items-center gap-2 justify-center bg-black text-white px-8 py-4 rounded-full font-medium text-[15px] hover:bg-neutral-800 transition-all shadow-md hover:shadow-xl hover:-translate-y-0.5 disabled:opacity-70 disabled:cursor-not-allowed disabled:transform-none disabled:shadow-none"
              >
                {loading ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Crunching Data...
                  </>
                ) : (
                  <>
                    <Search className="w-4 h-4 opacity-70" />
                    Extract Data
                    <ArrowRight className="w-4 h-4 opacity-0 -ml-4 group-hover:opacity-100 group-hover:ml-0 transition-all duration-300" />
                  </>
                )}
              </button>
            </div>
            
          </form>
        </div>

        {/* Preset tags below the card */}
        <div className="flex flex-wrap items-center justify-center gap-2 mt-6">
          <button onClick={() => setPreset("software_developer")} className="px-4 py-1.5 bg-white border border-neutral-200 text-neutral-500 text-[13px] font-medium rounded-full hover:bg-neutral-50 hover:text-neutral-900 transition-colors shadow-sm">
            Software Developer
          </button>
          <button onClick={() => setPreset("software_intern")} className="px-4 py-1.5 bg-white border border-neutral-200 text-neutral-500 text-[13px] font-medium rounded-full hover:bg-neutral-50 hover:text-neutral-900 transition-colors shadow-sm">
            Software Engineer Intern
          </button>
          <button onClick={() => setPreset("bcom_intern")} className="px-4 py-1.5 bg-white border border-neutral-200 text-neutral-500 text-[13px] font-medium rounded-full hover:bg-neutral-50 hover:text-neutral-900 transition-colors shadow-sm">
            B.COM / Finance Intern
          </button>
        </div>

      </motion.div>

      {/* Results / Results Downloader */}
      <AnimatePresence>
        {jobs.length > 0 && !loading && (
          <motion.div 
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="w-full max-w-6xl mt-16 text-center"
          >
            <div className="bg-white border border-neutral-200 p-8 rounded-4xl shadow-sm overflow-hidden">
              <div className="inline-flex items-center justify-center w-12 h-12 bg-[#F2FCE2] text-green-700 rounded-full mb-4">
                <Github strokeWidth={1.5} />
              </div>
              <h3 className="text-2xl font-serif mb-2">Success! Found {jobs.length} jobs.</h3>
              <p className="text-neutral-500 text-[15px] mb-8">
                Your data has been formatted and is ready for analysis.
              </p>
              
              <button 
                onClick={downloadCSV}
                className="inline-flex items-center gap-2 bg-neutral-100 text-neutral-900 px-6 py-3 rounded-full font-medium text-[14px] hover:bg-neutral-200 transition-colors border border-transparent hover:border-neutral-300 mb-8"
              >
                <Download className="w-4 h-4" /> Download as CSV
              </button>

              {/* Data Table */}
              <div className="overflow-x-auto border border-neutral-200 rounded-xl max-h-[500px]">
                <table className="w-full text-left text-sm whitespace-nowrap">
                  <thead className="bg-neutral-50 text-neutral-600 sticky top-0 uppercase tracking-wider text-xs">
                    <tr>
                      {getOrderedColumns().map((col) => (
                        <th key={col} className="px-4 py-3 border-b border-neutral-200 font-medium">{col.replace(/_/g, " ")}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-neutral-100">
                    {jobs.map((job, idx) => (
                      <tr key={job.id || idx} className="hover:bg-neutral-50/50 transition-colors">
                        {getOrderedColumns().map((col) => (
                          <td key={col} className="px-4 py-3 text-neutral-700 max-w-[250px] truncate">
                            {job[col] !== null && job[col] !== undefined ? String(job[col]) : "-"}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Bottom Stats Footer */}
      <div className="mt-auto pt-24 pb-8 flex items-center justify-center gap-12 sm:gap-24 opacity-80">
        <div className="text-center space-y-1">
          <div className="text-[20px] font-medium text-neutral-900">5k+</div>
          <div className="text-[10px] font-bold text-neutral-400 tracking-widest uppercase">JOBS SOURCED</div>
        </div>
        <div className="text-center space-y-1">
          <div className="text-[20px] font-medium text-neutral-900">Free</div>
          <div className="text-[10px] font-bold text-neutral-400 tracking-widest uppercase">ALWAYS</div>
        </div>
        <div className="text-center space-y-1">
          <div className="text-[20px] font-medium text-neutral-900">CSV</div>
          <div className="text-[10px] font-bold text-neutral-400 tracking-widest uppercase">EXPORT</div>
        </div>
      </div>

    </main>
  );
}
