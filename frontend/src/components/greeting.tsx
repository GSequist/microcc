import { useMemo } from 'react';
import { motion } from 'framer-motion';
import { Banner } from './banner';
import { useSession } from '../contexts/SessionContext';

export const Greeting = () => {
  const { session, hints } = useSession();
  // One tip per page load, from the shared registry (utils/hints.py).
  const tip = useMemo(() => (hints.length ? hints[Math.floor(Math.random() * hints.length)] : null), [hints]);

  return (
    <motion.div
      key="overview"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1, duration: 0.4 }}
      className="w-full flex flex-col items-start gap-3 px-4 select-none"
    >
      <Banner />

      {session && (
        <div className="flex flex-col items-start gap-0.5 font-mono text-[10px] text-muted-foreground/50">
          <span>{session.project_dir}</span>
          <span className="opacity-70">
            {session.model}
            {session.dangerous.length > 0 && ` · ${session.dangerous.length} tools gated`}
          </span>
        </div>
      )}

      {tip && <p className="font-mono text-[10px] text-muted-foreground/60">⌖ {tip}</p>}
    </motion.div>
  );
};
