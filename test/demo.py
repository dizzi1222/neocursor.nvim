# ============================================================
#  tabtab.nvim playground
#
#  APPEND demo (inline ghost text):
#    Put the cursor at the END of a stub in section 1, press `A` to
#    insert, pause — a dimmed completion appears. <Tab> accepts.
#
#  REWRITE demo (diff overlay):
#    add() below returns a - b, but the name says add. Move the cursor
#    onto the `return a - b` line, press `A`, then nudge the cursor
#    (type a space and delete it) to fire a request. Cursor proposes
#    `return a + b` as a red→green diff. <Tab> applies it.
# ============================================================


# ---- section 1: append ---------------------------------------------

def is_prime(n):
    # return True if n is prime, else False


def quicksort(arr):


# ---- section 2: rewrite --------------------------------------------

def add(a, b):
    return a - b
