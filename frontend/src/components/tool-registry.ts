import { getCopy } from '../envUtils';
import {
  LineChartIcon,
  GlobeIcon,
  FileIcon,
  AttachmentIcon,
  PenIcon,
  EyeIcon,
  ImageIcon,
  TerminalIcon,
  SummarizeIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  UploadIcon,
  PencilEditIcon,
  DatabaseIcon,
  InfoIcon,
  TableIcon,
  SearchIcon,
  EmailIcon,
  PaperclipIcon,
  PlusIcon,
  MessageIcon,
  OutlookIcon,
  MSTeamsIcon
} from './icons';

export const TOOL_ICONS: Record<string, any> = {
  // Planning
  'make_plan' : PenIcon,
  'update_step': PencilEditIcon,
  'add_step': PlusIcon,
  'advance_to_step': ArrowRightIcon,
  'show_full_plan': EyeIcon,

  // Memory
  'memory' : PenIcon,

  // Planning
  'todo_tool_': PenIcon,

  // Search & Web
  'web_search': GlobeIcon,
  'search_in_user_docs': AttachmentIcon,
  'archive_search': FileIcon,
  'search_on_sharepoint_vector': AttachmentIcon,
  'find_in_MIDO_index': AttachmentIcon,
  'find_in_katalog_chyb_': AttachmentIcon,
  'find_in_problem_records_': EyeIcon,
  'fetch_sharepoint_document': AttachmentIcon,
  'find_on_page': EyeIcon,
  'find_next': ArrowRightIcon,

  // Outlook
  'outlook_mail': OutlookIcon,
  'outlook_calendar': OutlookIcon,
  'teams' : MSTeamsIcon,

  // Navigation & Movement
  'visit_url': GlobeIcon,
  'download_from_url': UploadIcon,
  'page_up': ArrowUpIcon,
  'page_down': ArrowUpIcon,

  // Database & Data
  'execute_mysql_query': DatabaseIcon,
  'list_mysql_tables': DatabaseIcon,
  'describe_mysql_table': DatabaseIcon,
  'tableau_tool_get_fuzzy_metadata_vds': DatabaseIcon,
  'tableau_list_datasources': LineChartIcon,
  'tableau_tool_query_datasource_vds': LineChartIcon,
  "tableau_cached_report": LineChartIcon,

  // Search tools
  'search_tools': SearchIcon,

  // Skills
  'read_skill': SearchIcon,
  'list_skills': SearchIcon,

  // Research & Data
  'pubmed': DatabaseIcon,
  'manifold': DatabaseIcon,

  // Clinical trials & Regulatory
  'search_ct_gov_': DatabaseIcon,
  'search_eu_ct_': DatabaseIcon,
  'search_cteu_': DatabaseIcon,
  'fetch_trials': DatabaseIcon,
  'fetch_batch_trials': DatabaseIcon,
  'fetch_ct_gov_daily_': DatabaseIcon,
  'search_regulatory_documents': DatabaseIcon,
  'list_regulatory_countries': DatabaseIcon,
  'list_docs_per_country': DatabaseIcon,

  // Files & Documents
  'translate_doc': PenIcon,
  'legal_review': SummarizeIcon,
  'gen_report_for_admin': SummarizeIcon,

  // Technical & Systems
  'kernel': TerminalIcon,
  'launch_webapp': GlobeIcon,
  'vision': EyeIcon,

  // Creative & Generation
    'image_gen': ImageIcon,
};

const t = getCopy();

export const TOOL_LABELS: Record<string, string> = {
  // Planning
  'todo_tool_': t.todo_tool_,
  
  // Memory
  'memory': t.memory,

  // Skills & Tools
  'search_tools': t.search_tools,
  'read_skill': t.read_skill,
  'list_skills': t.list_skills,

  // Code & Technical
  'kernel': t.kernel,
  'launch_webapp': t.launch_webapp,
  'vision': t.vision,

  // Research & Data
  'manifold': t.manifold,

  // Web & Navigation
  'web_search': t.web_search,
  'visit_url': t.visit_url,
  'download_from_url': t.download_from_url,
  'page_up': t.page_up,
  'page_down': t.page_down,
  'find_on_page': t.find_on_page,
  'find_next': t.find_next,

  // Documents & Files
  'search_in_user_docs': t.search_in_user_docs,
  'search_on_sharepoint_vector': t.search_on_sharepoint_vector,
  'find_in_MIDO_index': t.find_in_MIDO_index,
  'search_vn_deviations': t.search_vn_deviations,
  'find_in_katalog_chyb_': t.find_in_katalog_chyb_,
  'find_in_problem_records_': t.find_in_problem_records_,
  'fetch_sharepoint_document': t.fetch_sharepoint_document,
  'text_file': t.text_file,
  'archive_search': t.archive_search,
  'legal_review': t.legal_review,
  'translate_doc': t.translate_doc,
  'gen_report_for_admin': t.gen_report_for_admin,

  // Outlook
  'outlook_mail': t.outlook_mail,
  'outlook_calendar': t.outlook_calendar,
  'teams': t.teams,

  // Database
  'execute_mysql_query': t.execute_mysql_query,
  'list_mysql_tables': t.list_mysql_tables,
  'describe_mysql_table': t.describe_mysql_table,

  // Tableau
  'tableau_list_datasources': t.tableau_list_datasources,
  'tableau_tool_get_fuzzy_metadata_vds': t.tableau_tool_get_fuzzy_metadata_vds,
  'tableau_tool_query_datasource_vds': t.tableau_tool_query_datasource_vds,
  "tableau_cached_report": t.tableau_cached_report,

  // Clinical Trials & Regulatory
  'search_ct_gov_': t.search_ct_gov_,
  'search_eu_ct_': t.search_eu_ct_,
  'search_cteu_': t.search_cteu_,
  'fetch_trials': t.fetch_trials,
  'fetch_batch_trials': t.fetch_batch_trials,
  'fetch_ct_gov_daily_': t.fetch_ct_gov_daily_,
  'search_regulatory_documents': t.search_regulatory_documents,
  'list_regulatory_countries': t.list_regulatory_countries,
  'list_docs_per_country': t.list_docs_per_country,

  // Creative
  'image_gen': t.image_gen,
};