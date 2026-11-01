import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/* The workspace's two masthead voices — solid ink and a quiet ruled outline — plus a bare icon button. */
const buttonVariants = cva("btn", {
  variants: {
    variant: {
      solid: "btn-solid",
      outline: "btn-outline",
      ghost: "btn-ghost",
    },
    size: {
      default: "btn-md",
      sm: "btn-sm",
      lg: "btn-lg",
      icon: "btn-icon",
    },
  },
  defaultVariants: {
    variant: "solid",
    size: "default",
  },
})

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button"
    return <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
  },
)
Button.displayName = "Button"

export { Button, buttonVariants }
