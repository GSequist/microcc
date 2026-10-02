import React, { useState } from "react";
import {
  motion,
  useTransform,
  AnimatePresence,
  useMotionValue,
  useSpring,
} from "framer-motion";

const AnimatedTooltip = ({ tool, icon }: { tool: any, icon: string }) => {
  const [isHovered, setIsHovered] = useState(false);
  const springConfig = { stiffness: 100, damping: 5 };
  const x = useMotionValue(0);
  const rotate = useSpring(
    useTransform(x, [-100, 100], [-45, 45]), 
    springConfig
  );
  const translateX = useSpring(
    useTransform(x, [-100, 100], [-50, 50]), 
    springConfig
  );

  const handleMouseMove = (event: React.MouseEvent<HTMLDivElement>) => {
    const halfWidth = (event.target as HTMLElement).offsetWidth / 2;
    x.set(event.nativeEvent.offsetX - halfWidth);
  };

  return (
    <div
      className="-mr-4 relative group"
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      <AnimatePresence mode="popLayout">
        {isHovered && (
          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.6 }}
            animate={{
              opacity: 1,
              y: 0,
              scale: 1,
              transition: {
                type: "spring",
                stiffness: 260,
                damping: 10,
              },
            }}
            exit={{ opacity: 0, y: 20, scale: 0.6 }}
            style={{
              translateX: translateX,
              rotate: rotate,
              whiteSpace: "nowrap",
            }}
            className="absolute -top-4 -left-0.1 translate-x-1/2 flex text-[10px] flex-col items-center justify-center rounded-sm bg-black z-50 shadow-md px-1.5 py-0.5"
            >
            <div className="font-medium text-white relative z-30 text-xs">
                {tool.toolName}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      <img
        onMouseMove={handleMouseMove}
        src={icon}
        alt={tool.toolName}
        className="object-cover !m-0 !p-0 object-top rounded-full h-10 w-10 border-2 group-hover:scale-105 group-hover:z-30 border-white relative transition duration-500"
      />
    </div>
  );
};

export default AnimatedTooltip;