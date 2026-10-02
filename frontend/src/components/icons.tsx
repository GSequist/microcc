// ─── Icons ───────────────────────────────────────────────────────
// Thin, minimal stroke icons via lucide-react, exposed through this
// facade so the ~37 call sites keep importing the same names.
//
// The single thinness knob lives in `make()` below: change `strokeWidth`
// once and every lucide icon re-weights. 1.5 is a clean thin at 14–16px;
// drop to 1.25 for wispier (can look fragile on 1× displays).
//
// Brand marks (logos) stay hand-rolled SVG at the bottom — they have no
// stroke equivalent and must keep their exact colors/geometry.

import {
  ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Bot, Box, Check, ChevronDown,
  CircleCheck, CircleHelp, Code, Command, Copy, Database, Download, Ellipsis,
  EllipsisVertical, Eye, File, Folder, Globe, History, House, Image, Info,
  LineChart, LoaderCircle, Lock, Logs, Mail, Maximize, Menu, MessageCircle,
  Navigation, PanelLeft, Paperclip, Pen, Pencil, Play, Plus, Receipt, Redo2, Route,
  ScrollText, Search, Share, Sparkles, Square, SquareCheck, SquareTerminal,
  Table, Terminal, ThumbsDown, ThumbsUp, ToggleLeft, ToggleRight, Triangle,
  TriangleAlert, Trash2, Undo2, Upload, User, X,
  type LucideIcon, type LucideProps,
} from 'lucide-react';

// Factory: wraps a lucide icon with our defaults. `size` defaults to 16
// (lucide's raw default is 24) and strokeWidth to 1.5 — the global thin knob.
// Any extra prop (className, style, onClick, strokeWidth override…) passes through.
const make = (Icon: LucideIcon) =>
  ({ size = 16, strokeWidth = 1.5, ...rest }: LucideProps) => (
    <Icon size={size} strokeWidth={strokeWidth} {...rest} />
  );

export const DatabaseIcon = make(Database);
export const BotIcon = make(Bot);
export const UserIcon = make(User);
export const AttachmentIcon = make(File);
export const BoxIcon = make(Box);
export const HomeIcon = make(House);
export const GPSIcon = make(Navigation);
export const InvoiceIcon = make(Receipt);
export const RouteIcon = make(Route);
export const FileIcon = make(File);
export const LoaderIcon = make(LoaderCircle);
export const UploadIcon = make(Upload);
export const MenuIcon = make(Menu);
export const PencilEditIcon = make(Pencil);
export const CheckedSquare = make(SquareCheck);
export const UncheckedSquare = make(Square);
export const MoreIcon = make(EllipsisVertical);
export const TrashIcon = make(Trash2);
export const InfoIcon = make(Info);
export const ArrowUpIcon = make(ArrowUp);
export const ArrowDownIcon = make(ArrowDown);
export const ArrowLeftIcon = make(ArrowLeft);
export const ArrowRightIcon = make(ArrowRight);
export const PaperclipIcon = make(Paperclip);
export const MoreHorizontalIcon = make(Ellipsis);
export const MessageIcon = make(MessageCircle);
export const EmailIcon = make(Mail);
export const CrossIcon = make(X);
export const CrossSmallIcon = make(X);
export const UndoIcon = make(Undo2);
export const RedoIcon = make(Redo2);
export const DeltaIcon = make(Triangle);
export const PenIcon = make(Pen);
export const SummarizeIcon = make(ScrollText);
export const CheckIcon = make(Check);          // was rendering a plus — now a real check
export const SidebarLeftIcon = make(PanelLeft);
export const PlusIcon = make(Plus);
export const CopyIcon = make(Copy);
export const TableIcon = make(Table);          // was rendering a plus — now a real table
export const ThumbUpIcon = make(ThumbsUp);
export const ThumbDownIcon = make(ThumbsDown);
export const ChevronDownIcon = make(ChevronDown);
export const SparklesIcon = make(Sparkles);
export const CheckCircleFillIcon = make(CircleCheck);  // now outline, not filled
export const GlobeIcon = make(Globe);
export const LockIcon = make(Lock);
export const EyeIcon = make(Eye);
export const ShareIcon = make(Share);
export const CodeIcon = make(Code);
export const PlayIcon = make(Play);
export const TerminalWindowIcon = make(SquareTerminal);
export const TerminalIcon = make(Terminal);
export const ClockRewind = make(History);
export const LogsIcon = make(Logs);
export const ImageIcon = make(Image);
export const FullscreenIcon = make(Maximize);
export const DownloadIcon = make(Download);
export const LineChartIcon = make(LineChart);
export const WarningIcon = make(TriangleAlert);
export const FolderIcon = make(Folder);
export const CommandIcon = make(Command);
export const HelpIcon = make(CircleHelp);
export const SearchIcon = make(Search);
export const ToggleOffIcon = make(ToggleLeft);
export const ToggleOnIcon = make(ToggleRight);

// ─── Brand marks & primitives kept as raw SVG ──────────────────────
// No lucide equivalent (logos) or need a filled shape (StopIcon).

export const RainbowIcon = ({ size = 16 }: { size: number }) => {
      return (
      <svg
        data-testid="geist-icon"
        height={size}
        width={size}
        strokeLinejoin="round"
        viewBox="0 0 16 16"
        style={{ color: 'currentcolor' }}
      >
        <path
          d="M9 7L12.5 2.5"
          stroke="#E5484D"
          style={{ stroke: '#E5484D', strokeOpacity: 1 }}
          strokeWidth="1.5"
        />
        <path
          d="M10.5 9.5L15.75 10.5"
          stroke="#52AEFF"
          style={{ stroke: '#52AEFF', strokeOpacity: 1 }}
          strokeWidth="1.5"
        />
        <path
          d="M10 8L15.75 6"
          stroke="#45DEC4"
          style={{ stroke: '#45DEC4', strokeOpacity: 1 }}
          strokeWidth="1.5"
        />
        <path
          fillRule="evenodd"
          clipRule="evenodd"
          d="M6.14568 3.56625L7 2L7.85432 3.56625L12.1818 11.5L13 13H11.2914H2.70863H1L1.81818 11.5L3.31818 8.75H0V7.25H4.13636L6.14568 3.56625ZM3.52681 11.5L7 5.13249L10.4732 11.5H3.52681Z"
          fill="currentColor"
        />
      </svg>
  );
};

export const GitIcon = () => {
  return (
    <svg
      height="16"
      strokeLinejoin="round"
      viewBox="0 0 16 16"
      width="16"
      style={{ color: 'currentcolor' }}
    >
      <g clipPath="url(#clip0_872_3147)">
        <path
          fillRule="evenodd"
          clipRule="evenodd"
          d="M8 0C3.58 0 0 3.57879 0 7.99729C0 11.5361 2.29 14.5251 5.47 15.5847C5.87 15.6547 6.02 15.4148 6.02 15.2049C6.02 15.0149 6.01 14.3851 6.01 13.7154C4 14.0852 3.48 13.2255 3.32 12.7757C3.23 12.5458 2.84 11.836 2.5 11.6461C2.22 11.4961 1.82 11.1262 2.49 11.1162C3.12 11.1062 3.57 11.696 3.72 11.936C4.44 13.1455 5.59 12.8057 6.05 12.5957C6.12 12.0759 6.33 11.726 6.56 11.5261C4.78 11.3262 2.92 10.6364 2.92 7.57743C2.92 6.70773 3.23 5.98797 3.74 5.42816C3.66 5.22823 3.38 4.40851 3.82 3.30888C3.82 3.30888 4.49 3.09895 6.02 4.1286C6.66 3.94866 7.34 3.85869 8.02 3.85869C8.7 3.85869 9.38 3.94866 10.02 4.1286C11.55 3.08895 12.22 3.30888 12.22 3.30888C12.66 4.40851 12.38 5.22823 12.3 5.42816C12.81 5.98797 13.12 6.69773 13.12 7.57743C13.12 10.6464 11.25 11.3262 9.47 11.5261C9.76 11.776 10.01 12.2558 10.01 13.0056C10.01 14.0752 10 14.9349 10 15.2049C10 15.4148 10.15 15.6647 10.55 15.5847C12.1381 15.0488 13.5182 14.0284 14.4958 12.6673C15.4735 11.3062 15.9996 9.67293 16 7.99729C16 3.57879 12.42 0 8 0Z"
          fill="currentColor"
        />
      </g>
      <defs>
        <clipPath id="clip0_872_3147">
          <rect width="16" height="16" fill="white" />
        </clipPath>
      </defs>
    </svg>
  );
};

export const StopIcon = ({ size = 16 }: { size?: number }) => {
  return (
    <svg
      height={size}
      viewBox="0 0 16 16"
      width={size}
      style={{ color: 'currentcolor' }}
    >
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M3 3H13V13H3V3Z"
        fill="currentColor"
      />
    </svg>
  );
};

export const LogoOpenAI = ({size  =16}: {size?: number}) => {
  return(
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      style={{ color: 'currentcolor' }}
      role="img" xmlns="http://www.w3.org/2000/svg">
      <path fill="currentColor" d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/>
    </svg>
  )
}

export const LogoClaude = ({ size = 16 }: { size?: number }) => {
  return (
   <svg
    width={size}
    height={size}
    style={{ display: 'inline', margin: '0 4px'}}
    xmlns="http://www.w3.org/2000/svg"
    shapeRendering="geometricPrecision"
    textRendering="geometricPrecision"
    imageRendering="optimizeQuality"
    fillRule="evenodd"
    clipRule="evenodd"
    viewBox="0 0 512 509.64">
    <path fill="#D77655" d="M115.612 0h280.775C459.974 0 512 52.026 512 115.612v278.415c0 63.587-52.026 115.612-115.613 115.612H115.612C52.026 509.639 0 457.614 0 394.027V115.612C0 52.026 52.026 0 115.612 0z"/>
    <path fill="#FCF2EE" fillRule="nonzero" d="M142.27 316.619l73.655-41.326 1.238-3.589-1.238-1.996-3.589-.001-12.31-.759-42.084-1.138-36.498-1.516-35.361-1.896-8.897-1.895-8.34-10.995.859-5.484 7.482-5.03 10.717.935 23.683 1.617 35.537 2.452 25.782 1.517 38.193 3.968h6.064l.86-2.451-2.073-1.517-1.618-1.517-36.776-24.922-39.81-26.338-20.852-15.166-11.273-7.683-5.687-7.204-2.451-15.721 10.237-11.273 13.75.935 3.513.936 13.928 10.716 29.749 23.027 38.848 28.612 5.687 4.727 2.275-1.617.278-1.138-2.553-4.271-21.13-38.193-22.546-38.848-10.035-16.101-2.654-9.655c-.935-3.968-1.617-7.304-1.617-11.374l11.652-15.823 6.445-2.073 15.545 2.073 6.547 5.687 9.655 22.092 15.646 34.78 24.265 47.291 7.103 14.028 3.791 12.992 1.416 3.968 2.449-.001v-2.275l1.997-26.641 3.69-32.707 3.589-42.084 1.239-11.854 5.863-14.206 11.652-7.683 9.099 4.348 7.482 10.716-1.036 6.926-4.449 28.915-8.72 45.294-5.687 30.331h3.313l3.792-3.791 15.342-20.372 25.782-32.227 11.374-12.789 13.27-14.129 8.517-6.724 16.1-.001 11.854 17.617-5.307 18.199-16.581 21.029-13.75 17.819-19.716 26.54-12.309 21.231 1.138 1.694 2.932-.278 44.536-9.479 24.062-4.347 28.714-4.928 12.992 6.066 1.416 6.167-5.106 12.613-30.71 7.583-36.018 7.204-53.636 12.689-.657.48.758.935 24.164 2.275 10.337.556h25.301l47.114 3.514 12.309 8.139 7.381 9.959-1.238 7.583-18.957 9.655-25.579-6.066-59.702-14.205-20.474-5.106-2.83-.001v1.694l17.061 16.682 31.266 28.233 39.152 36.397 1.997 8.999-5.03 7.102-5.307-.758-34.401-25.883-13.27-11.651-30.053-25.302-1.996-.001v2.654l6.926 10.136 36.574 54.975 1.895 16.859-2.653 5.485-9.479 3.311-10.414-1.895-21.408-30.054-22.092-33.844-17.819-30.331-2.173 1.238-10.515 113.261-4.929 5.788-11.374 4.348-9.478-7.204-5.03-11.652 5.03-23.027 6.066-30.052 4.928-23.886 4.449-29.674 2.654-9.858-.177-.657-2.173.278-22.37 30.71-34.021 45.977-26.919 28.815-6.445 2.553-11.173-5.789 1.037-10.337 6.243-9.2 37.257-47.392 22.47-29.371 14.508-16.961-.101-2.451h-.859l-98.954 64.251-17.618 2.275-7.583-7.103.936-11.652 3.589-3.791 29.749-20.474-.101.102.024.101z"/>
    </svg>
  );
};

export const LogoGoogle = ({ size = 16 }: { size?: number }) => {
  return (
    <svg
      data-testid="geist-icon"
      height={size}
      strokeLinejoin="round"
      viewBox="0 0 16 16"
      width={size}
      style={{ color: 'currentcolor' }}
    >
      <path
        d="M8.15991 6.54543V9.64362H12.4654C12.2763 10.64 11.709 11.4837 10.8581 12.0509L13.4544 14.0655C14.9671 12.6692 15.8399 10.6182 15.8399 8.18188C15.8399 7.61461 15.789 7.06911 15.6944 6.54552L8.15991 6.54543Z"
        fill="#4285F4"
      />
      <path
        d="M3.6764 9.52268L3.09083 9.97093L1.01807 11.5855C2.33443 14.1963 5.03241 16 8.15966 16C10.3196 16 12.1305 15.2873 13.4542 14.0655L10.8578 12.0509C10.1451 12.5309 9.23598 12.8219 8.15966 12.8219C6.07967 12.8219 4.31245 11.4182 3.67967 9.5273L3.6764 9.52268Z"
        fill="#34A853"
      />
      <path
        d="M1.01803 4.41455C0.472607 5.49087 0.159912 6.70543 0.159912 7.99995C0.159912 9.29447 0.472607 10.509 1.01803 11.5854C1.01803 11.5926 3.6799 9.51991 3.6799 9.51991C3.5199 9.03991 3.42532 8.53085 3.42532 7.99987C3.42532 7.46889 3.5199 6.95983 3.6799 6.47983L1.01803 4.41455Z"
        fill="#FBBC05"
      />
      <path
        d="M8.15982 3.18545C9.33802 3.18545 10.3853 3.59271 11.2216 4.37818L13.5125 2.0873C12.1234 0.792777 10.3199 0 8.15982 0C5.03257 0 2.33443 1.79636 1.01807 4.41455L3.67985 6.48001C4.31254 4.58908 6.07983 3.18545 8.15982 3.18545Z"
        fill="#EA4335"
      />
    </svg>
  );
};

export const LogoAnthropic = () => {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      xmlnsXlink="http://www.w3.org/1999/xlink"
      x="0px"
      y="0px"
      viewBox="0 0 92.2 65"
      style={{ color: 'currentcolor', fill: 'currentcolor' }}
      width="18px"
      height="18px"
    >
      <path
        d="M66.5,0H52.4l25.7,65h14.1L66.5,0z M25.7,0L0,65h14.4l5.3-13.6h26.9L51.8,65h14.4L40.5,0C40.5,0,25.7,0,25.7,0zM24.3,39.3l8.8-22.8l8.8,22.8H24.3z"
      />
    </svg>
  );
};

export const PythonIcon = ({ size = 16 }: { size?: number }) => {
  return (
    <svg
      height={size}
      strokeLinejoin="round"
      viewBox="0 0 16 16"
      width={size}
      style={{ color: 'currentcolor' }}
    >
      <path
        d="M7.90474 0.00013087C7.24499 0.00316291 6.61494 0.0588153 6.06057 0.15584C4.42745 0.441207 4.13094 1.0385 4.13094 2.14002V3.59479H7.9902V4.07971H4.13094H2.68259C1.56099 4.07971 0.578874 4.7465 0.271682 6.01496C-0.0826597 7.4689 -0.0983767 8.37619 0.271682 9.89434C0.546012 11.0244 1.20115 11.8296 2.32276 11.8296H3.64966V10.0856C3.64966 8.82574 4.75179 7.71441 6.06057 7.71441H9.91533C10.9884 7.71441 11.845 6.84056 11.845 5.77472V2.14002C11.845 1.10556 10.9626 0.328487 9.91533 0.15584C9.25237 0.046687 8.56448 -0.00290121 7.90474 0.00013087ZM5.81768 1.17017C6.21631 1.17017 6.54185 1.49742 6.54185 1.89978C6.54185 2.30072 6.21631 2.62494 5.81768 2.62494C5.41761 2.62494 5.09351 2.30072 5.09351 1.89978C5.09351 1.49742 5.41761 1.17017 5.81768 1.17017Z"
        fill="currentColor"
      ></path>
      <path
        d="M12.3262 4.07971V5.77472C12.3262 7.08883 11.1997 8.19488 9.91525 8.19488H6.06049C5.0046 8.19488 4.13086 9.0887 4.13086 10.1346V13.7693C4.13086 14.8037 5.04033 15.4122 6.06049 15.709C7.28211 16.0642 8.45359 16.1285 9.91525 15.709C10.8868 15.4307 11.8449 14.8708 11.8449 13.7693V12.3145H7.99012V11.8296H11.8449H13.7745C14.8961 11.8296 15.3141 11.0558 15.7041 9.89434C16.1071 8.69865 16.0899 7.5488 15.7041 6.01495C15.4269 4.91058 14.8975 4.07971 13.7745 4.07971H12.3262ZM10.1581 13.2843C10.5582 13.2843 10.8823 13.6086 10.8823 14.0095C10.8823 14.4119 10.5582 14.7391 10.1581 14.7391C9.7595 14.7391 9.43397 14.4119 9.43397 14.0095C9.43397 13.6086 9.7595 13.2843 10.1581 13.2843Z"
        fill="currentColor"
      ></path>
    </svg>
  );
};

export const OutlookIcon = ({ size = 16 } : { size?: number}) => {
  return (
    <svg
    width={size}
    height={size}
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    >
    <rect x="10" y="2" width="20" height="28" rx="2" fill="#1066B5"/>
    <rect x="10" y="2" width="20" height="28" rx="2" fill="url(#paint0_linear_87_7742)"/>
    <rect x="10" y="5" width="10" height="10" fill="#32A9E7"/>
    <rect x="10" y="15" width="10" height="10" fill="#167EB4"/>
    <rect x="20" y="15" width="10" height="10" fill="#32A9E7"/>
    <rect x="20" y="5" width="10" height="10" fill="#58D9FD"/>
    <mask id="mask0_87_7742" maskUnits="userSpaceOnUse" x="8" y="14" width="24" height="16">
    <path d="M8 14H30C31.1046 14 32 14.8954 32 16V28C32 29.1046 31.1046 30 30 30H10C8.89543 30 8 29.1046 8 28V14Z" fill="url(#paint1_linear_87_7742)"/>
    </mask>
    <g mask="url(#mask0_87_7742)">
    <path d="M32 14V18H30V14H32Z" fill="#135298"/>
    <path d="M32 30V16L7 30H32Z" fill="url(#paint2_linear_87_7742)"/>
    <path d="M8 30V16L33 30H8Z" fill="url(#paint3_linear_87_7742)"/>
    </g>
    <path d="M8 12C8 10.3431 9.34315 9 11 9H17C18.6569 9 20 10.3431 20 12V24C20 25.6569 18.6569 27 17 27H8V12Z" fill="#000000" fillOpacity="0.3"/>
    <rect y="7" width="18" height="18" rx="2" fill="url(#paint4_linear_87_7742)"/>
    <path d="M14 16.0693V15.903C14 13.0222 11.9272 11 9.01582 11C6.08861 11 4 13.036 4 15.9307V16.097C4 18.9778 6.07278 21 9 21C11.9114 21 14 18.964 14 16.0693ZM11.6424 16.097C11.6424 18.0083 10.5665 19.1579 9.01582 19.1579C7.46519 19.1579 6.37342 17.9806 6.37342 16.0693V15.903C6.37342 13.9917 7.44937 12.8421 9 12.8421C10.5348 12.8421 11.6424 14.0194 11.6424 15.9307V16.097Z" fill="white"/>
    <defs>
    <linearGradient id="paint0_linear_87_7742" x1="10" y1="16" x2="30" y2="16" gradientUnits="userSpaceOnUse">
    <stop stopColor="#064484"/>
    <stop offset="1" stopColor="#0F65B5"/>
    </linearGradient>
    <linearGradient id="paint1_linear_87_7742" x1="8" y1="26.7692" x2="32" y2="26.7692" gradientUnits="userSpaceOnUse">
    <stop stopColor="#1B366F"/>
    <stop offset="1" stopColor="#2657B0"/>
    </linearGradient>
    <linearGradient id="paint2_linear_87_7742" x1="32" y1="23" x2="8" y2="23" gradientUnits="userSpaceOnUse">
    <stop stopColor="#44DCFD"/>
    <stop offset="0.453125" stopColor="#259ED0"/>
    </linearGradient>
    <linearGradient id="paint3_linear_87_7742" x1="8" y1="23" x2="32" y2="23" gradientUnits="userSpaceOnUse">
    <stop stopColor="#259ED0"/>
    <stop offset="1" stopColor="#44DCFD"/>
    </linearGradient>
    <linearGradient id="paint4_linear_87_7742" x1="0" y1="16" x2="18" y2="16" gradientUnits="userSpaceOnUse">
    <stop stopColor="#064484"/>
    <stop offset="1" stopColor="#0F65B5"/>
    </linearGradient>
    </defs>
    </svg>
  )
}

export const MSTeamsIcon = ({ size = 16 } : { size?: number}) => {
  return (
    <svg
    width={size}
    height={size}
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    >
    <path d="M19 13.9032C19 13.4044 19.4044 13 19.9032 13H31.0968C31.5956 13 32 13.4044 32 13.9032V20.5C32 24.0899 29.0899 27 25.5 27C21.9101 27 19 24.0899 19 20.5V13.9032Z" fill="url(#paint0_linear_87_7777)"/>
    <path d="M9 12.2258C9 11.5488 9.54881 11 10.2258 11H23.7742C24.4512 11 25 11.5488 25 12.2258V22C25 26.4183 21.4183 30 17 30C12.5817 30 9 26.4183 9 22V12.2258Z" fill="url(#paint1_linear_87_7777)"/>
    <circle cx="27" cy="8" r="3" fill="#34439E"/>
    <circle cx="27" cy="8" r="3" fill="url(#paint2_linear_87_7777)"/>
    <circle cx="18" cy="6" r="4" fill="url(#paint3_linear_87_7777)"/>
    <mask id="mask0_87_7777"  maskUnits="userSpaceOnUse" x="9" y="0" width="16" height="30">
    <path d="M17 10C19.7615 10 22 7.76147 22 5C22 2.23853 19.7615 0 17 0C14.2385 0 12 2.23853 12 5C12 7.76147 14.2385 10 17 10Z" fill="url(#paint4_linear_87_7777)"/>
    <path d="M10.2258 11C9.54883 11 9 11.5488 9 12.2258V22C9 26.4183 12.5817 30 17 30C21.4183 30 25 26.4183 25 22V12.2258C25 11.5488 24.4512 11 23.7742 11H10.2258Z" fill="url(#paint5_linear_87_7777)"/>
    </mask>
    <g mask="url(#mask0_87_7777)">
    <path d="M7 12C7 10.3431 8.34315 9 10 9H17C18.6569 9 20 10.3431 20 12V24C20 25.6569 18.6569 27 17 27H7V12Z" fill="#000000" fillOpacity="0.3"/>
    </g>
    <rect y="7" width="18" height="18" rx="2" fill="url(#paint6_linear_87_7777)"/>
    <path d="M13 11H5V12.8347H7.99494V21H10.0051V12.8347H13V11Z" fill="white"/>
    <defs>
    <linearGradient id="paint0_linear_87_7777" x1="19" y1="13.7368" x2="32.1591" y2="22.3355" gradientUnits="userSpaceOnUse">
    <stop stopColor="#364088"/>
    <stop offset="1" stopColor="#6E7EE1"/>
    </linearGradient>
    <linearGradient id="paint1_linear_87_7777" x1="9" y1="19.4038" x2="25" y2="19.4038" gradientUnits="userSpaceOnUse">
    <stop stopColor="#515FC4"/>
    <stop offset="1" stopColor="#7084EA"/>
    </linearGradient>
    <linearGradient id="paint2_linear_87_7777" x1="24" y1="5.31579" x2="29.7963" y2="9.39469" gradientUnits="userSpaceOnUse">
    <stop stopColor="#364088"/>
    <stop offset="1" stopColor="#6E7EE1"/>
    </linearGradient>
    <linearGradient id="paint3_linear_87_7777" x1="15.1429" y1="3.14286" x2="20.2857" y2="9.14286" gradientUnits="userSpaceOnUse">
    <stop stopColor="#4858AE"/>
    <stop offset="1" stopColor="#4E60CE"/>
    </linearGradient>
    <linearGradient id="paint4_linear_87_7777" x1="13.4286" y1="1.42857" x2="19.8571" y2="8.92857" gradientUnits="userSpaceOnUse">
    <stop stopColor="#4858AE"/>
    <stop offset="1" stopColor="#4E60CE"/>
    </linearGradient>
    <linearGradient id="paint5_linear_87_7777" x1="13.4286" y1="1.42857" x2="19.8571" y2="8.92857" gradientUnits="userSpaceOnUse">
    <stop stopColor="#4858AE"/>
    <stop offset="1" stopColor="#4E60CE"/>
    </linearGradient>
    <linearGradient id="paint6_linear_87_7777" x1="-5.21539e-08" y1="16" x2="18" y2="16" gradientUnits="userSpaceOnUse">
    <stop stopColor="#2A3887"/>
    <stop offset="1" stopColor="#4C56B9"/>
    </linearGradient>
    </defs>
    </svg>
  )
}
