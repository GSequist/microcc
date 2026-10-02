import { motion } from 'framer-motion';
import { Banner } from './banner';
import { useSession } from '../contexts/SessionContext';

export const Greeting = () => {
  const { session } = useSession();

  return (
    <motion.div
      key="overview"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1, duration: 0.4 }}
      className="w-full flex flex-col items-start gap-4 px-4 select-none"
    >
      <Banner />

      {session && (
        <div className="flex flex-col items-start gap-1 font-mono text-[11px] text-muted-foreground/50">
          <span>{session.project_dir}</span>
          <span className="opacity-70">
            {session.model}
            {session.dangerous.length > 0 && ` · ${session.dangerous.length} tools gated`}
          </span>
        </div>
      )}
    </motion.div>
  );
};
