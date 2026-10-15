# Linux 시스템 콜 따라가기 — fopen()/fread()에서 ext4까지

**시스템 소프트웨어 26년 2학기 · 팀 보고서**

---

## 목차

1. [개요 및 분석 환경](#1-개요-및-분석-환경)
2. [분석 대상 프로그램](#2-분석-대상-프로그램)
3. [A: fopen() 경로](#3-a-fopen-경로)
   - A-1. 사용자 공간: glibc stdio에서 syscall 직전까지
   - A-2. 커널 진입: syscall 명령에서 시스템 콜 함수까지
   - A-3. VFS: 경로 탐색과 struct file 생성
   - A-4. ext4: inode 읽기와 ext4_file_open
   - A-5. 복귀: fd가 FILE*이 되기까지
4. [B: fread() 경로](#4-b-fread-경로)
   - B-1. 사용자 공간: stdio 버퍼링
   - B-2. VFS: read 시스템 콜
   - B-3. ext4와 페이지 캐시
   - B-4. 복귀와 두 번째 fread
5. [실험 검증 (E1~E4)](#5-실험-검증)
6. [핵심 개념 정리](#6-핵심-개념-정리)
7. [사용 도구](#7-사용-도구)

---

## 1. 개요 및 분석 환경

### 분석 목표

C 표준 라이브러리 함수 `fopen()`과 `fread()` 한 번의 호출이 **glibc → x86-64 syscall 진입부 → Linux VFS → ext4** 까지 이르는 전체 경로를 소스 코드와 실행 추적으로 확인한다.

### 전체 계층 구조

```
┌─────────────────────────────────────────┐  사용자 공간
│  fopen()  →  glibc stdio 초기화         │
│  fread()  →  glibc stdio 버퍼 확인       │
├─────────────────────────────────────────┤
│  INLINE_SYSCALL (syscall 명령)           │  ← 사용자/커널 경계
├─────────────────────────────────────────┤  커널 공간
│  entry_SYSCALL_64 → do_syscall_64       │
│  __x64_sys_openat / __x64_sys_read      │
├─────────────────────────────────────────┤
│  VFS: do_sys_openat2 / vfs_read         │
│       경로 탐색, struct file 생성        │
├─────────────────────────────────────────┤
│  ext4: ext4_file_open                   │
│         ext4_file_read_iter             │  ← 과제 필수 범위
├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
│  블록 계층 → 디스크 드라이버 (가산점)     │  ← 선택 범위
└─────────────────────────────────────────┘
```

### 분석 기준 환경

| 항목 | 고정 조건 | 비고 |
|------|-----------|------|
| CPU 아키텍처 | x86-64 (AMD64), 64비트 | ARM64, 32비트 int 0x80 경로는 분석하지 않음 |
| 커널 소스 기준 | Linux v6.12 (6.12.y LTS) | 인용 형식: `fs/open.c:1234 @v6.12` |
| C 라이브러리 | glibc 2.39 | Bootlin Elixir glibc-2.39 참조 |
| 운영체제 | Ubuntu 24.04 LTS x86-64 | 기본 glibc 2.39 |
| 파일 시스템 | ext4, data=ordered | `df -T .`로 ext4 확인 |
| 열기·읽기 방식 | `fopen(path, "r")`, 버퍼링 기본값 | O_DIRECT, mmap, setvbuf 변경 제외 |
| 컴파일 | `gcc -O0 -g`, glibc 동적 링크 | `-static` 금지 |

---

## 2. 분석 대상 프로그램

```c
/* fsread.c */
#include <stdio.h>

int main(int argc, char *argv[])
{
    const char *path = (argc > 1) ? argv[1] : "data.txt";
    char buf[100];

    FILE *fp = fopen(path, "r");          /* 과제 A */
    if (fp == NULL) {
        perror("fopen");
        return 1;
    }

    size_t n = fread(buf, 1, sizeof(buf), fp);   /* 과제 B: 첫 번째 fread */
    size_t m = fread(buf, 1, sizeof(buf), fp);   /* 과제 B: 두 번째 fread */
    printf("read %zu + %zu bytes\n", n, m);

    fclose(fp);
    return 0;
}
```

**빌드 및 데이터 준비:**

```bash
seq 1 6000 > data.txt        # 약 28KB 파일 생성 (seq 출력: "1\n2\n...\n6000\n")
gcc -O0 -g -o fsread fsread.c
./fsread data.txt
# 출력 예시: read 100 + 100 bytes
```

`data.txt`는 약 28 KB(6000줄 × 평균 4.7자)이므로, `fread(buf, 1, 100, fp)`를 두 번 호출해도 파일 끝에 닿지 않는다.

---

## 3. A: fopen() 경로

### 전체 호출 트리 (A 경로 요약)

```
fopen("data.txt", "r")
└── _IO_new_fopen             libio/iofopen.c
    └── __fopen_internal
        ├── _IO_no_init           (FILE 구조체 초기화)
        ├── _IO_JUMPS_FILE_plus 설정 (vtable 등록)
        └── _IO_new_file_fopen    libio/fileops.c
            ├── 모드 "r" → O_RDONLY | _IO_NO_WRITES 변환
            └── _IO_file_open     libio/fileops.c
                ├── __open_nocancel(path, O_RDONLY, 0666)
                │   └── __libc_open64  sysdeps/.../open64.c
                │       └── SYSCALL_CANCEL(openat, AT_FDCWD, path, flags, mode)
                │           └── INLINE_SYSCALL / internal_syscall4
                │               └── syscall 명령  ← 커널 진입
                └── _IO_link_in   (전역 FILE 목록 등록)
```

---

### A-1. 사용자 공간: glibc stdio에서 syscall 직전까지

#### fopen → _IO_new_fopen (libio/iofopen.c)

`fopen`은 glibc에서 `_IO_new_fopen`의 **버전 심볼 별칭(versioned symbol alias)**이다.

```c
// libio/iofopen.c
FILE *
_IO_new_fopen (const char *filename, const char *mode)
{
  return __fopen_internal (filename, mode, 1);
}

versioned_symbol (libc, _IO_new_fopen, fopen, GLIBC_2_1);
```

- `fopen`과 `fopen64`는 x86-64(64비트)에서 **같은 함수**로 처리된다. x86-64는 기본적으로 `_FILE_OFFSET_BITS=64`이므로 `fopen64`가 `fopen`의 별칭이 된다(`libio/iofopen.c`의 `weak_alias`).
- 32비트 x86에서는 `fopen`(32비트 오프셋)과 `fopen64`(64비트 오프셋)가 구분된다.

#### __fopen_internal (libio/iofopen.c)

`FILE`과 잠금·vtable을 담는 `_IO_FILE_plus` 구조체를 힙에 할당하고 초기화한다.

```c
FILE *
__fopen_internal (const char *filename, const char *mode, int is32)
{
  struct locked_FILE {
    struct _IO_FILE_plus fp;
    ...
  } *new_f = (struct locked_FILE *) malloc (sizeof (struct locked_FILE));

  _IO_no_init (&new_f->fp.file, 0, 0, &new_f->ld, &_IO_file_jumps);
  // → _IO_JUMPS(&new_f->fp) = &_IO_file_jumps (vtable 등록)

  _IO_new_file_init_internal (&new_f->fp);
  // → fp.file._fileno = -1, 포인터들 NULL, 잠금 초기화

  if (_IO_file_fopen ((FILE *) new_f, filename, mode, is32) != NULL)
    return __fopen_maybe_mmap (&new_f->fp.file); // mmap 가능하면 전환
  ...
}
```

- `_IO_no_init`: 버퍼 포인터(`_IO_read_base` 등), 상태 플래그, vtable(`_IO_JUMPS`)을 초기화
- `_IO_JUMPS`: `FILE` 구조체에서 vtable 포인터를 꺼내는 매크로
- `_IO_new_file_init_internal`: `_fileno = -1`, 연결 리스트 초기화

#### _IO_new_file_fopen (libio/fileops.c)

모드 문자열 `"r"`을 OS 플래그로 변환한다.

```c
FILE *
_IO_new_file_fopen (FILE *fp, const char *filename, const char *mode, int is32bit)
{
  int oflags = 0, omode;
  ...
  switch (*mode++) {
    case 'r':
      omode = O_RDONLY;       // 읽기 전용
      read_write = _IO_NO_WRITES;
      break;
    case 'w': ...
    case 'a': ...
  }
  // '+' 가 붙으면 O_RDWR로 변경
  ...
  return _IO_file_open (fp, filename, oflags, omode, read_write, is32bit);
}
```

| 모드 문자열 | oflags | _IO 플래그 |
|------------|--------|------------|
| `"r"` | `O_RDONLY` | `_IO_NO_WRITES` |
| `"w"` | `O_WRONLY\|O_CREAT\|O_TRUNC` | `_IO_NO_READS` |
| `"r+"` | `O_RDWR` | 없음 |

#### _IO_file_open (libio/fileops.c)

실제 `open` 시스템 콜을 호출하고 fd를 `FILE`에 연결한다.

```c
FILE *
_IO_file_open (FILE *fp, const char *filename, int posix_mode, int prot,
               int read_write, int is32bit)
{
  int fdesc;
  ...
  // 취소 가능 여부에 따라 분기
  fdesc = __open_nocancel (filename, posix_mode, prot);
  // 또는: fdesc = __open (filename, posix_mode, prot);
  
  if (fdesc < 0) return NULL;
  
  fp->_fileno = fdesc;          // fd를 FILE에 저장
  ...
  _IO_link_in ((struct _IO_FILE_plus *) fp);  // 전역 FILE 목록에 등록
  return fp;
}
```

- **`__open_nocancel` vs `__open`**: `__open_nocancel`은 비동기 취소(pthread cancel) 도중 `open`이 중단되지 않도록 한다. `SYSCALL_CANCEL` 매크로로 구현되며, 취소 지점을 명시적으로 처리한다.
- `_IO_link_in`: `_IO_list_all`이라는 전역 연결 리스트에 이 `FILE`을 등록한다 (fflush, exit 시 전체 스트림 처리에 쓰임).

#### __libc_open64 (sysdeps/unix/sysv/linux/open64.c)

```c
int
__libc_open64 (const char *file, int oflag, ...)
{
  ...
  return SYSCALL_CANCEL (openat, AT_FDCWD, file, oflag | O_LARGEFILE, mode);
}
```

- `open`이 왜 `openat(AT_FDCWD, ...)`으로 바뀌는가: Linux 커널 2.6.16부터 `open`이 `openat`의 특수 경우로 통합되었다. `AT_FDCWD`는 "현재 작업 디렉터리 기준"을 의미하는 특수 상수(`-100`)다. glibc는 `open`을 직접 호출하지 않고 `openat`에 `AT_FDCWD`를 넘긴다.
- `O_LARGEFILE`: 64비트 파일 크기를 지원하기 위해 자동 추가된다.
- `SYSCALL_CANCEL`: 스레드 취소 지점을 등록하는 매크로. 내부적으로 `INLINE_SYSCALL`을 감싼다.

#### INLINE_SYSCALL / internal_syscall4 (sysdeps/unix/sysv/linux/x86_64/sysdep.h)

이 부분은 **함수가 아닌 인라인 어셈블리**다.

```c
// x86-64 syscall 호출 규약 (openat = 시스템 콜 번호 257)
//
// rax = 시스템 콜 번호 (257 = __NR_openat)
// rdi = 1번 인자: dfd      (AT_FDCWD = -100)
// rsi = 2번 인자: filename ("data.txt" 주소)
// rdx = 3번 인자: flags    (O_RDONLY | O_LARGEFILE = 0x8000)
// r10 = 4번 인자: mode     (0666)
//
// (x86-64 리눅스 syscall 규약: 6개 인자는 rdi, rsi, rdx, r10, r8, r9)
// (일반 함수 호출과의 차이: 4번째 인자가 rcx 아닌 r10)

#define internal_syscall4(number, arg1, arg2, arg3, arg4)   \
({                                                           \
  long int resultvar;                                        \
  register long int _a4 asm ("r10") = (arg4);               \
  asm volatile (                                             \
    "syscall\n\t"                                            \
    : "=a" (resultvar)                                       \
    : "0" (number), "D" (arg1), "S" (arg2), "d" (arg3),     \
      "r" (_a4)                                              \
    : "memory", "rcx", "r11", "cc"                          \
  );                                                         \
  resultvar;                                                 \
})
```

| 레지스터 | 역할 | openat 예시 |
|---------|------|------------|
| `rax` | 시스템 콜 번호 | `257` (`__NR_openat`) |
| `rdi` | 1번 인자 | `AT_FDCWD` (`-100`) |
| `rsi` | 2번 인자 | `"data.txt"` 주소 |
| `rdx` | 3번 인자 | `O_RDONLY\|O_LARGEFILE` |
| `r10` | 4번 인자 (rcx 대신) | `0666` |
| `rcx` | **syscall 명령이 rip 저장** | (파괴됨) |
| `r11` | **syscall 명령이 rflags 저장** | (파괴됨) |

`syscall` 명령 직후 반환값은 `rax`에 있다. 음수(`-4095` ~ `-1`)이면 오류이고, glibc는 `errno = -rax`로 설정한 뒤 `-1`을 반환한다.

---

### A-2. 커널 진입: syscall 명령에서 시스템 콜 함수까지

#### (CPU) syscall 명령

`syscall`은 x86-64 전용 명령으로 인터럽트 없이 빠르게 커널로 진입한다.

**하드웨어가 하는 일:**

1. `RCX ← RIP` (복귀 주소 저장)
2. `R11 ← RFLAGS` (플래그 저장)
3. `RIP ← MSR_LSTAR` (커널 진입점으로 점프)
4. `CS ← IA32_STAR[47:32]` (커널 코드 세그먼트, CPL=0으로 전환)
5. `RFLAGS &= ~IA32_FMASK` (IF 비트 클리어 → 인터럽트 금지)

**MSR_LSTAR 설정 (커널 초기화 시):**

```c
// arch/x86/kernel/cpu/common.c
void syscall_init(void)
{
    wrmsr(MSR_STAR, 0, (__USER32_CS << 16) | __KERNEL_CS);
    wrmsrl(MSR_LSTAR, (unsigned long)entry_SYSCALL_64);  // ← 진입점 등록
    wrmsrl(MSR_CSTAR, (unsigned long)entry_SYSCALL_compat);
    ...
    wrmsrl(MSR_SYSCALL_MASK, X86_EFLAGS_TF|X86_EFLAGS_DF|X86_EFLAGS_IF
                             |X86_EFLAGS_IOPL|X86_EFLAGS_AC|X86_EFLAGS_NT);
}
```

즉 `syscall` 명령을 실행하면 CPU는 자동으로 `entry_SYSCALL_64`로 점프한다.

#### entry_SYSCALL_64 (arch/x86/entry/entry_64.S)

```asm
SYM_CODE_START(entry_SYSCALL_64)
    swapgs                    /* GS 베이스를 커널용(pcpu)으로 교환 */
    movq %rsp, PER_CPU_VAR(cpu_tss_rw + TSS_sp2) /* 사용자 rsp 임시 저장 */
    movq PER_CPU_VAR(cpu_current_top_of_stack), %rsp /* 커널 스택으로 전환 */

    /* 사용자 레지스터를 struct pt_regs 형식으로 스택에 쌓기 */
    pushq $__USER_DS                /* ss */
    pushq PER_CPU_VAR(cpu_tss_rw + TSS_sp2) /* rsp (사용자) */
    pushq %r11                      /* rflags (syscall이 저장한 값) */
    pushq $__USER_CS                /* cs */
    pushq %rcx                      /* rip (syscall이 저장한 복귀 주소) */
    pushq %rax                      /* orig_rax (시스템 콜 번호) */
    pushq %rdi
    pushq %rsi
    pushq %rdx
    pushq %rcx                      /* 이미 RIP용으로 쓴 rcx, 덮어씀 */
    pushq $-ENOSYS                  /* rax 자리 (임시) */
    pushq %r8
    pushq %r9
    pushq %r10
    pushq %r11
    ...
    PUSH_AND_CLEAR_REGS             /* rbx, rbp, r12~r15, ...*/

    movq %rsp, %rdi
    call do_syscall_64              /* C 함수로 이동 */
```

- **`swapgs`**: `gs` 세그먼트 베이스를 사용자 TLS에서 커널의 `per-cpu` 데이터 영역으로 교환한다. 커널 스택 주소 등 per-CPU 정보에 접근하기 위해 필요하다.
- **커널 스택 전환**: `cpu_current_top_of_stack`에서 이 CPU의 현재 커널 스택 주소를 가져온다.
- **`struct pt_regs`**: 스택에 쌓인 사용자 레지스터 묶음. `rdi`가 첫 번째 인자로 이 구조체의 주소를 받아 `do_syscall_64`에 전달한다.
- **PTI (Page Table Isolation)**: Meltdown 취약점 대응. `CR3_SWITCH_TO_KERNEL_CR3` 매크로가 사용자/커널 페이지 테이블을 전환한다. 역할은 "커널 주소를 사용자 페이지 테이블에서 숨기는 것"이다.

#### do_syscall_64 (arch/x86/entry/common.c)

```c
__visible noinstr void do_syscall_64(struct pt_regs *regs, int nr)
{
    add_random_kstack_offset();
    nr = syscall_enter_from_user_mode(regs, nr);  // seccomp, ptrace 등

    if (likely(nr < NR_syscalls)) {               // 번호 범위 검사 (NR_syscalls = 512)
        nr = array_index_nospec(nr, NR_syscalls); // spectre 완화
        regs->ax = x64_sys_call(regs, nr);        // 디스패치 ★
    }

    syscall_exit_to_user_mode(regs);              // 복귀 준비
}
```

**복귀 시 sysret vs iret 결정:**

- `syscall_exit_to_user_mode` → `exit_to_user_mode_prepare` 안에서 조건 확인
- **`sysret`**: CS가 정상 `__USER_CS`이고, `RIP`가 정식 사용자 주소이며, `TF` 플래그가 없으면 사용. 빠름.
- **`iret`**: 시그널 처리, ptrace, 스택 조작 등 특수 상황. 모든 세그먼트 레지스터를 복원하므로 느림.

#### x64_sys_call (arch/x86/entry/syscall_64.c)

```c
// arch/x86/entry/syscall_64.c (자동 생성 포함)
long x64_sys_call(const struct pt_regs *regs, unsigned int nr)
{
    switch (nr) {
    case 0: return __x64_sys_read(regs);
    case 1: return __x64_sys_write(regs);
    ...
    case 257: return __x64_sys_openat(regs);    // openat = 257
    ...
    }
}
```

이 함수는 `arch/x86/entry/syscalls/syscall_64.tbl`을 파싱하여 빌드 시 **자동 생성**된다.

```
# syscall_64.tbl 일부
0    common  read    sys_read
1    common  write   sys_write
...
257  common  openat  sys_openat
```

#### __x64_sys_openat (fs/open.c, SYSCALL_DEFINE4)

```c
SYSCALL_DEFINE4(openat, int, dfd, const char __user *, filename,
                int, flags, umode_t, mode)
{
    ...
    return do_sys_open(dfd, filename, flags, mode);
}
```

`SYSCALL_DEFINEn` 매크로는 **세 가지 함수**를 만든다 (`arch/x86/include/asm/syscall_wrapper.h`):

| 함수 이름 | 역할 |
|----------|------|
| `__do_sys_openat(dfd, filename, flags, mode)` | 실제 구현 (개발자가 작성) |
| `__se_sys_openat(pt_regs *)` | `pt_regs`에서 인자 추출 후 `__do_sys_*` 호출 |
| `__x64_sys_openat(pt_regs *)` | `__se_sys_*`를 호출하는 아키텍처별 래퍼 |

`pt_regs`에서 인자를 꺼내는 방식:

```c
// arch/x86/include/asm/syscall_wrapper.h
#define SC_X86_64_REGS_TO_ARGS(x, ...)          \
    __MAP(x, __SC_ARGS                           \
          ,,regs->di,,regs->si,,regs->dx         \
          ,,regs->r10,,regs->r8,,regs->r9)
// → dfd = regs->di, filename = regs->si,
//   flags = regs->dx, mode = regs->r10
```

---

### A-3. VFS: 경로 탐색과 struct file 생성

#### do_sys_open → do_sys_openat2 (fs/open.c)

```
do_sys_open (fs/open.c:1427)
└── do_sys_openat2 (fs/open.c:1399)
    ├── build_open_flags(how, &op)        :1403  O_RDONLY 등 → 내부 플래그
    ├── getname(filename)                 :1409  사용자 메모리의 "data.txt" → 커널 복사
    ├── get_unused_fd_flags(how->flags)   :1413  빈 fd 번호 예약 (보통 3)
    ├── do_filp_open(dfd, tmp, &op)       :1415  실제 열기
    ├── fd_install(fd, f)                 :1420  fd 테이블[fd] = f
    └── return fd                               → pt_regs->ax → rax로 사용자에게
```

- `do_sys_open(:1427)`: `flags`를 `struct open_how`로 묶어 `do_sys_openat2`에 넘기는 얇은 래퍼
- 실패하면 `put_unused_fd`로 예약을 취소하고 음수 오류 코드 반환
- `fd_install(fs/file.c:588)`: `rcu_assign_pointer(fdt->fd[fd], file)`로 RCU 안전하게 등록

#### do_filp_open (fs/namei.c:3952): RCU 모드 재시도

```c
struct file *do_filp_open(int dfd, struct filename *pathname,
                          const struct open_flags *op)
{
    struct nameidata nd;
    ...
    filp = path_openat(&nd, op, flags | LOOKUP_RCU);     // 1차: 잠금 없이 빠르게
    if (unlikely(filp == ERR_PTR(-ECHILD)))
        filp = path_openat(&nd, op, flags);              // 2차: 참조·잠금을 잡고 다시
    if (unlikely(filp == ERR_PTR(-ESTALE)))
        filp = path_openat(&nd, op, flags | LOOKUP_REVAL); // 3차: 캐시 재검증
    ...
}
```

- **RCU 모드(`LOOKUP_RCU`)**: dentry에 참조 카운트를 잡지 않고 읽기만 한다. 빠르지만 dcache 미스나 심볼릭 링크 등에서 실패한다. 실패 시 `try_to_unlazy`로 그 자리에서 일반 모드로 전환을 시도한다. 전환도 실패하면 `-ECHILD`를 반환하고 처음부터 재시도한다.

#### path_openat (fs/namei.c:3913)

```c
static struct file *path_openat(struct nameidata *nd,
                                const struct open_flags *op, unsigned flags)
{
    file = alloc_empty_file(op->open_flag, current_cred());  // :3919 빈 struct file 생성
    ...
    const char *s = path_init(nd, flags);                    // :3928 시작 디렉터리 결정
    while (!(error = link_path_walk(s, nd)) &&               // :3929 마지막 성분 직전까지
           (s = open_last_lookups(nd, file, op)) != NULL)    //       마지막 성분 찾기
        ;
    if (!error)
        error = do_open(nd, file, op);                       // :3933 권한 검사 + 열기
    ...
}
```

`struct file`은 경로를 찾기 **전에** 먼저 만들어진다. 탐색이 실패하면 `fput`으로 버린다.

#### path_init (fs/namei.c:2439): AT_FDCWD 해석

```c
// fs/namei.c:2489 부근
if (!(nd->flags & LOOKUP_RCU))
    ...
// dfd == AT_FDCWD이면:
nd->path = current->fs->pwd;   // 현재 작업 디렉터리를 시작점으로
nd->inode = nd->path.dentry->d_inode;
```

glibc가 `open`을 `openat(AT_FDCWD, ...)`으로 바꾼 결과가 여기서 쓰인다. `/`로 시작하는 절대 경로면 `current->fs->root`를 시작점으로 삼는다.

#### link_path_walk (fs/namei.c:2343)

경로를 `/` 단위로 자르며 성분마다 처리한다:

1. `may_lookup`: 디렉터리에 대한 실행(x) 권한 검사
2. `walk_component`: dcache에서 해당 성분 찾기 (마지막 성분 직전까지)
3. 마지막 성분을 만나면 **찾지 않고** `nd->last`에 이름만 남기고 반환

> **본 실험의 경로 `"data.txt"`는 성분이 하나뿐이다.** 따라서 `walk_component`는 호출되지 않고, 곧바로 마지막 성분 처리(`open_last_lookups`)로 넘어간다. `/home/user/data.txt`처럼 열면 `home`, `user`에 대해 `walk_component`가 실행된다.

#### open_last_lookups (fs/namei.c:3647): "data.txt" 찾기

```c
// fs/namei.c:3665
lookup_fast_for_open(nd, open_flag)   // → lookup_fast → __d_lookup_rcu
```

1. `lookup_fast_for_open(:3665)` → `lookup_fast(:1656)` → `__d_lookup_rcu(:1667)`: dcache 해시 테이블에서 `(부모 dentry, 이름)`으로 검색. **찾으면 디스크에 가지 않는다.**
2. dcache에 없으면 RCU 모드를 벗어나 `lookup_open(:3694)` 으로 간다. 새 dentry를 만들고 `dir_inode->i_op->lookup(...)(:3573)`을 호출한다. ext4 디렉터리의 `i_op->lookup`은 `ext4_lookup`이다.

> 과제 표의 `lookup_fast`는 v6.12에서 `lookup_fast_for_open`을 거쳐 호출된다.

#### do_open (fs/namei.c:3730) → may_open (:3328)

```c
static int do_open(struct nameidata *nd, struct file *file,
                   const struct open_flags *op)
{
    ...
    error = may_open(idmap, &nd->path, acc_mode, open_flag); // :3772 권한 검사
    if (!error)
        error = vfs_open(&nd->path, file);                   // :3774
    ...
}
```

`may_open` 안의 `inode_permission(idmap, inode, MAY_OPEN | acc_mode)`이 "읽기 권한이 있는가"를 판단한다.

#### vfs_open (fs/open.c:1083) → do_dentry_open (fs/open.c:907)

VFS가 ext4로 넘어가는 핵심 지점이다.

```c
static int do_dentry_open(struct file *f, struct inode *inode, ...)
{
    ...
    f->f_inode   = inode;
    f->f_mapping = inode->i_mapping;             // 페이지 캐시(address_space) 연결

    if (S_ISREG(inode->i_mode) || S_ISDIR(inode->i_mode))
        f->f_mode |= FMODE_ATOMIC_POS;           // read의 fdget_pos와 연결

    f->f_op = fops_get(inode->i_fop);            // :939 ★ 함수 테이블 주소 복사

    if (!open)
        open = f->f_op->open;                    // :956 ★ = ext4_file_open
    if (open) {
        error = open(inode, f);                  // 함수 포인터 호출 → ext4_file_open
    }
    ...
    f->f_mode |= FMODE_OPENED;
    if ((f->f_mode & FMODE_READ) &&
         likely(f->f_op->read || f->f_op->read_iter))
        f->f_mode |= FMODE_CAN_READ;             // vfs_read의 검사와 연결
}
```

`vfs_open`은 `do_dentry_open(file, NULL)`로 호출하므로 `open` 인자가 `NULL`이 되고, 따라서 `f->f_op->open`이 사용된다.

---

### A-4. ext4: inode 읽기와 ext4_file_open

#### ext4_lookup (fs/ext4/namei.c:1788) → ext4_iget

dcache에 `"data.txt"`가 없을 때만 호출된다.

```c
// fs/ext4/namei.c:1788
static struct dentry *ext4_lookup(struct inode *dir, struct dentry *dentry, ...)
{
    struct inode *inode = NULL;
    ext4_lookup_entry(dir, dentry, &bh, &de); // 디렉터리 블록에서 이름 → inode 번호
    if (de) {
        inode = ext4_iget(dir->i_sb, ino, EXT4_IGET_NORMAL); // :1813
    }
    return d_splice_alias(inode, dentry);
}
```

`ext4_iget`은 `__ext4_iget(fs/ext4/inode.c:4694)`을 호출하는 매크로다.

#### __ext4_iget의 i_fop 설정 (fs/ext4/inode.c:4958)

```c
// fs/ext4/inode.c:4958
if (S_ISREG(inode->i_mode)) {                       // 일반 파일이면
    inode->i_op  = &ext4_file_inode_operations;
    inode->i_fop = &ext4_file_operations;           // :4960 ★ 함수 테이블 주소 등록
    ext4_set_aops(inode);                           // 페이지 캐시 연산(readahead 등) 연결
} else if (S_ISDIR(inode->i_mode)) {
    inode->i_op  = &ext4_dir_inode_operations;
    inode->i_fop = &ext4_dir_operations;            // 디렉터리용 다른 테이블
}
```

이 `i_fop`가 `do_dentry_open`에서 `f->f_op`로 복사된다(주소만 복사, 테이블은 전역에 하나).

#### ext4_file_operations (fs/ext4/file.c:929)

```c
const struct file_operations ext4_file_operations = {
    .llseek         = ext4_llseek,
    .read_iter      = ext4_file_read_iter,  // ★ .read는 없음(= NULL)
    .write_iter     = ext4_file_write_iter,
    .iopoll         = iocb_bio_iopoll,
    .unlocked_ioctl = ext4_ioctl,
    .mmap           = ext4_file_mmap,
    .open           = ext4_file_open,       // ★
    .release        = ext4_release_file,
    .fsync          = ext4_sync_file,
    .get_unmapped_area = thp_get_unmapped_area,
    .splice_read    = ext4_file_splice_read,
    .splice_write   = iter_file_splice_write,
    .fallocate      = ext4_fallocate,
};
```

`.read`가 없다(= `NULL`). C99 지정 초기화에서 적지 않은 멤버는 `NULL`이다. 이것이 뒤에서 VFS가 `read_iter`로 우회하는 이유가 된다.

#### ext4_file_open (fs/ext4/file.c:858)

| 순서 | 코드 | 역할 | 본 환경 |
|------|------|------|---------|
| 1 | `ext4_forced_shutdown(inode->i_sb)` | 파일 시스템 강제 종료 상태면 `-EIO` | 통과 |
| 2 | `ext4_sample_last_mounted(...)` | 슈퍼블록에 마지막 마운트 지점 기록 | 첫 open에서만 기록, 이후 플래그로 건너뜀 |
| 3 | `fscrypt_file_open(inode, filp)` | 암호화 파일 키 확인 | 조건 불충족으로 건너뜀 |
| 4 | `fsverity_file_open(inode, filp)` | verity 파일 검사 | 조건 불충족으로 건너뜀 |
| 5 | `if (filp->f_mode & FMODE_WRITE)` → ext4_inode_attach_jinode | 쓰기용이면 저널 inode 연결 | `"r"`이므로 건너뜀 |
| 6 | `filp->f_mode \|= FMODE_NOWAIT \| FMODE_CAN_ODIRECT` | 비동기·O_DIRECT 가능 표시 | 실행 |
| 7 | `return dquot_file_open(inode, filp)` | 디스크 쿼터 처리 | 실행 |

#### dquot_file_open → generic_file_open (fs/open.c:1615)

```c
// fs/quota/dquot.c:2211
int dquot_file_open(struct inode *inode, struct file *file)
{
    int error = generic_file_open(inode, file);
    if (!error && (file->f_mode & FMODE_WRITE))
        error = dquot_initialize(inode);   // 읽기 전용이므로 건너뜀
    return error;
}

// fs/open.c:1615
int generic_file_open(struct inode *inode, struct file *filp)
{
    if (!(filp->f_flags & O_LARGEFILE) && i_size_read(inode) > MAX_NON_LFS)
        return -EOVERFLOW;   // 2GB 초과 파일을 O_LARGEFILE 없이 열면 거부
    return 0;
}
```

> x86-64에서는 `SYSCALL_DEFINE4(openat)`이 `force_o_largefile()`이 참이면 자동으로 `O_LARGEFILE`을 붙이므로(`fs/open.c:1444~1445`) 이 검사는 통과한다. `data.txt`는 약 28 KB이므로 크기로도 통과한다.

---

### A-5. 복귀: fd가 FILE*이 되기까지

```
ext4_file_open() → 0 반환
  → do_dentry_open: FMODE_OPENED, FMODE_CAN_READ 설정
    → path_openat: struct file 반환
      → do_sys_openat2: fd_install(fd, f) 호출
        → fd 번호 반환 (예: 3)
          → pt_regs->ax = 3
            → sysret → rax = 3 (사용자 공간으로 복귀)
              → INLINE_SYSCALL 반환값 = 3
                → __libc_open64 반환 = 3
                  → _IO_file_open: fp->_fileno = 3 저장
                    → _IO_new_file_fopen: FILE * 반환
                      → fopen() 반환: FILE * (성공)
```

**실패 경로 (파일이 없을 때):**
- `lookup_open`에서 ext4_lookup이 `-ENOENT`를 반환
- `do_sys_openat2`가 `put_unused_fd`로 fd 예약 취소 후 음수 코드 반환
- `rax` = `-ENOENT`(= `-2`)
- glibc: `errno = ENOENT`, 반환값 `-1`
- `_IO_file_open`이 `NULL` 반환 → `fopen()` 반환값 `NULL`
- 프로그램에서 `if (fp == NULL) { perror("fopen"); ... }` 분기 실행

---

## 4. B: fread() 경로

### 전체 호출 트리 (B 경로 요약)

```
fread(buf, 1, 100, fp)          [첫 번째 fread]
└── _IO_fread                   libio/iofread.c
    └── _IO_acquire_lock(fp)    (FILE 잠금)
        └── _IO_sgetn(fp, buf, 100)
            └── _IO_XSGETN(fp, buf, 100)    (vtable 간접 호출)
                └── _IO_new_file_xsgetn     libio/fileops.c
                    └── [버퍼 없음 → 할당]
                        └── _IO_doallocbuf → _IO_file_doallocate
                            └── fstat → st_blksize → 버퍼 크기 결정 (보통 4096)
                        └── __underflow → _IO_new_file_underflow
                            └── _IO_file_read → __read
                                └── SYSCALL_CANCEL(read, fd, buf, nbytes)
                                    └── syscall 명령 (커널 진입)
```

---

### B-1. 사용자 공간: stdio 버퍼링

#### fread → _IO_fread (libio/iofread.c)

```c
size_t
_IO_fread (void *buf, size_t size, size_t count, FILE *fp)
{
    size_t bytes_requested = size * count;    // 100 × 1 = 100 바이트
    if (bytes_requested == 0)
        return 0;

    _IO_acquire_lock (fp);                    // 스레드 안전성을 위한 잠금
    bytes_read = _IO_sgetn (fp, (char *) buf, bytes_requested);
    _IO_release_lock (fp);

    // 완전한 원소 수 반환 (size가 1이면 bytes_read와 동일)
    return bytes_requested == bytes_read ? count : bytes_read / size;
}
```

#### _IO_sgetn → _IO_XSGETN (libio/genops.c, libio/libioP.h)

```c
// libio/genops.c
size_t
_IO_sgetn (FILE *fp, void *data, size_t n)
{
    return _IO_XSGETN (fp, data, n);   // vtable을 통한 간접 호출
}

// libio/libioP.h
#define _IO_XSGETN(FP, DATA, N) \
    JUMP2 (__xsgetn, FP, DATA, N)
// → fp->vtable->__xsgetn(fp, data, n)
// → _IO_file_jumps.xsgetn = _IO_file_xsgetn
```

**vtable 검증(`IO_validate_vtable`)의 보안상 이유**: glibc 2.24부터 도입. 공격자가 `FILE` 구조체를 변조해 vtable 포인터를 악의적인 함수로 바꾸는 공격(`FILE structure exploitation`)을 막는다. vtable 포인터가 glibc 내부의 유효한 vtable 영역을 가리키는지 확인한다.

#### _IO_new_file_xsgetn (libio/fileops.c)

이 함수가 버퍼링의 핵심 로직을 담당한다.

```c
static size_t
_IO_new_file_xsgetn (FILE *fp, void *data, size_t n)
{
    size_t want = n;

    // 1. 버퍼가 없으면 먼저 할당
    if (fp->_IO_buf_base == NULL)
        _IO_doallocbuf(fp);

    while (want > 0) {
        // 버퍼에 남은 데이터가 있으면 먼저 복사
        size_t have = fp->_IO_read_end - fp->_IO_read_ptr;
        if (have > 0) {
            size_t copy = (have < want) ? have : want;
            memcpy(data, fp->_IO_read_ptr, copy);
            fp->_IO_read_ptr += copy;
            data = (char*)data + copy;
            want -= copy;
        }
        if (want == 0) break;

        // 버퍼보다 큰 요청: 사용자 버퍼로 직접 읽기
        if (want >= fp->_IO_buf_end - fp->_IO_buf_base) {
            // _IO_SYSREAD로 직접 읽기
            ...
        } else {
            // 버퍼를 채운 후 복사
            if (__underflow(fp) == EOF) break;
        }
    }
    return n - want;
}
```

**버퍼 관련 포인터 구조:**

```
_IO_buf_base ──► [         버퍼 메모리 영역           ] ◄── _IO_buf_end
_IO_read_base ──► [소비 완료] [아직 안 읽은 데이터] ◄── _IO_read_end
                              ↑
                        _IO_read_ptr  (다음에 읽을 위치)
```

#### _IO_doallocbuf → _IO_file_doallocate (libio/genops.c, libio/filedoalloc.c)

```c
// libio/filedoalloc.c
int
_IO_file_doallocate (FILE *fp)
{
    size_t size;
    struct stat64 st;

    // fstat로 파일 정보를 읽어 st_blksize를 버퍼 크기로 사용
    if (_IO_SYSSTAT(fp, &st) == 0 && st.st_blksize > 0)
        size = st.st_blksize;  // 보통 4096 (ext4 기본 블록 크기)
    else
        size = _IO_BUFSIZ;     // 기본값 8192

    char *p = malloc(size);
    SETUP_FILE_BUFS(fp, p, size);  // _IO_buf_base, _IO_buf_end 설정
    return 1;
}
```

> **이때 `fstat` 계열 시스템 콜이 발생한다.** `strace`로 확인하면 첫 번째 `fread` 전에 `newfstatat` 또는 `fstat` 호출이 보인다.

#### __underflow → _IO_new_file_underflow (libio/genops.c, libio/fileops.c)

```c
// libio/fileops.c
int
_IO_new_file_underflow (FILE *fp)
{
    if (fp->_flags & _IO_NO_READS)   // 쓰기 전용이면 거부
        return EOF;
    if (fp->_IO_read_ptr < fp->_IO_read_end)  // 버퍼에 데이터 있으면 그냥 반환
        return *(unsigned char *) fp->_IO_read_ptr;

    // 버퍼 채우기: _IO_read_base부터 전체 버퍼 크기만큼 읽기
    fp->_IO_read_base = fp->_IO_read_ptr = fp->_IO_buf_base;
    fp->_IO_read_end = fp->_IO_buf_base;

    count = _IO_SYSREAD(fp, fp->_IO_buf_base,
                        fp->_IO_buf_end - fp->_IO_buf_base);  // 버퍼 크기(4096) 요청
    fp->_IO_read_end += count;

    return *(unsigned char *) fp->_IO_read_ptr;
}
```

**`_IO_read_ptr` / `_IO_read_end` 갱신:**
- 읽기 전: `_IO_read_ptr == _IO_read_base == _IO_buf_base`, `_IO_read_end == _IO_buf_base`
- 4096바이트 읽은 후: `_IO_read_end = _IO_buf_base + 4096`
- 100바이트 복사 후: `_IO_read_ptr = _IO_buf_base + 100`

#### _IO_file_read → __read (libio/fileops.c, sysdeps/unix/sysv/linux/read.c)

```c
// libio/fileops.c
ssize_t
_IO_file_read (FILE *fp, void *buf, ssize_t size)
{
    return __read (fp->_fileno, buf, size);
}

// sysdeps/unix/sysv/linux/read.c
ssize_t
__libc_read (int fd, void *buf, size_t nbytes)
{
    return SYSCALL_CANCEL (read, fd, buf, nbytes);
}
```

> **커널에 요청하는 바이트 수는 100이 아니다.** 4096(또는 `st_blksize`) 바이트를 요청한다. stdio 버퍼 전체를 한 번에 채우는 것이다. 프로그램이 100바이트를 원한다고 해도 glibc는 미래의 fread 호출을 위해 더 많이 미리 읽어둔다. → 이것이 **사용자 공간 버퍼링**의 핵심 효과다.

---

### B-2. VFS: read 시스템 콜

커널 진입(`entry_SYSCALL_64`, `do_syscall_64`)은 A-2와 동일한 구조이므로 생략. read의 시스템 콜 번호는 `0`(`__NR_read`)이고, 인자는 `rdi=fd`, `rsi=buf`, `rdx=count`이다.

#### __x64_sys_read → ksys_read (fs/read_write.c)

```c
// fs/read_write.c:701
ssize_t ksys_read(unsigned int fd, char __user *buf, size_t count)
{
    struct fd f = fdget_pos(fd);    // :703 fd 번호 → struct file
    if (fd_file(f)) {
        loff_t pos, *ppos = file_ppos(fd_file(f));   // &file->f_pos
        if (ppos) { pos = *ppos; ppos = &pos; }      // 지역 변수로 복사
        ret = vfs_read(fd_file(f), buf, count, ppos);
        if (ret >= 0 && ppos)
            fd_file(f)->f_pos = pos;   // 읽은 만큼 전진한 위치를 되돌려 씀
        fdput_pos(f);
    }
    ...
}
```

**`fdget_pos` 잠금 조건:**  
`fdget_pos(fs/file.c:1153)`는 `FMODE_ATOMIC_POS`가 있고 참조가 둘 이상이거나 `iterate_shared`(디렉터리용)가 있을 때만 `f_pos_lock` 뮤텍스를 잡는다. `fsread`에서는 스레드가 하나이고 `fork·dup`도 없어 **잠금 없이** 지나간다. 여러 스레드가 같은 fd를 공유할 때 `f_pos`가 엉키지 않게 하는 장치다.

#### vfs_read (fs/read_write.c:549)

```c
ssize_t vfs_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
    if (!(file->f_mode & FMODE_READ))      return -EBADF;   // "r"로 열었으므로 통과
    if (!(file->f_mode & FMODE_CAN_READ))  return -EINVAL;  // do_dentry_open이 설정
    if (unlikely(!access_ok(buf, count)))  return -EFAULT;  // buf가 사용자 주소 범위인가
    ret = rw_verify_area(READ, file, pos, count);           // 오프셋·크기, LSM 보안 훅
    if (count > MAX_RW_COUNT) count = MAX_RW_COUNT;

    if (file->f_op->read)                                   // :566 ext4는 NULL → 거짓
        ret = file->f_op->read(file, buf, count, pos);
    else if (file->f_op->read_iter)
        ret = new_sync_read(file, buf, count, pos);         // :569 ★ 이쪽
    else
        ret = -EINVAL;

    if (ret > 0) {
        fsnotify_access(file);   // inotify 등에 "읽힘" 알림
        add_rchar(current, ret); // 프로세스 I/O 통계
    }
    ...
}
```

**ext4의 `.read`는 `NULL`이다.** 따라서 `f->f_op->read` 검사는 거짓이 되어 `new_sync_read`로 간다. 이 연결은 `ext4_file_operations`에 `.read` 필드가 없는 것에서 비롯된다.

#### new_sync_read (fs/read_write.c:478)

```c
static ssize_t new_sync_read(struct file *filp, char __user *buf,
                             size_t len, loff_t *ppos)
{
    struct kiocb kiocb;
    struct iov_iter iter;

    init_sync_kiocb(&kiocb, filp);              // 어느 파일, 동기 I/O
    kiocb.ki_pos = (ppos ? *ppos : 0);          // 어느 위치부터
    iov_iter_ubuf(&iter, ITER_DEST, buf, len);  // 사용자 버퍼 주소 + 길이

    ret = filp->f_op->read_iter(&kiocb, &iter); // :488 ★ = ext4_file_read_iter
    if (ppos)
        *ppos = kiocb.ki_pos;                   // 읽은 만큼 전진한 위치
    return ret;
}
```

이 함수는 `(버퍼, 길이, 위치)` 형식을 `(kiocb, iov_iter)` 형식으로 변환하는 **어댑터**다. `iov_iter`를 쓰면 `read`, `readv`, 비동기 I/O를 파일 시스템 쪽에서 함수 하나로 처리할 수 있다.

> **여기 도착하는 `buf`, `len`은 프로그램의 `buf[100]`, `100`이 아니다.** glibc stdio 버퍼의 주소와 크기(4096)이다.

---

### B-3. ext4와 페이지 캐시

#### ext4_file_read_iter (fs/ext4/file.c:130)

```c
static ssize_t ext4_file_read_iter(struct kiocb *iocb, struct iov_iter *to)
{
    if (unlikely(ext4_forced_shutdown(inode->i_sb))) return -EIO;   // 강제 종료
    if (!iov_iter_count(to))                          return 0;      // 0바이트

#ifdef CONFIG_FS_DAX
    if (IS_DAX(inode)) return ext4_dax_read_iter(iocb, to);  // 조건 불충족 → 건너뜀
#endif
    if (iocb->ki_flags & IOCB_DIRECT)
        return ext4_dio_read_iter(iocb, to);     // O_DIRECT 아님 → 건너뜀

    return generic_file_read_iter(iocb, to);     // ★ 본 환경의 경로
}
```

#### generic_file_read_iter → filemap_read (mm/filemap.c)

`generic_file_read_iter(mm/filemap.c:2792)`도 `IOCB_DIRECT`가 아니므로 곧바로 `filemap_read(iocb, iter, 0)(:2833)`을 호출한다.

```c
// mm/filemap.c:2610
ssize_t filemap_read(struct kiocb *iocb, struct iov_iter *iter, ssize_t already_read)
{
    do {
        if (unlikely(iocb->ki_pos >= i_size_read(inode)))
            break;                                              // 파일 끝이면 종료

        error = filemap_get_pages(iocb, iter->count, &fbatch, false); // :2645 folio 확보

        for (i = 0; i < folio_batch_count(&fbatch); i++) {
            ...
            copied = copy_folio_to_iter(folio, offset, bytes, iter); // :2696 사용자로 복사
            already_read += copied;
            iocb->ki_pos += copied;                             // 위치 전진
        }
    } while (iov_iter_count(iter) && iocb->ki_pos < isize && !error); // :2711

    return already_read ? already_read : error;
}
```

종료 조건 세 가지: ① 요청한 만큼 다 채움, ② 파일 끝, ③ 오류

#### filemap_get_pages (mm/filemap.c:2523): 캐시 히트와 미스

```c
pgoff_t index = iocb->ki_pos >> PAGE_SHIFT;           // 바이트 오프셋 → 페이지 번호
last_index = DIV_ROUND_UP(iocb->ki_pos + count, PAGE_SIZE);

filemap_get_read_batch(mapping, index, last_index - 1, fbatch);  // 캐시 조회 (xarray)

if (!folio_batch_count(fbatch)) {                     // 하나도 없으면 = 캐시 미스
    page_cache_sync_readahead(mapping, ra, filp, index,
                              last_index - index);    // :2547 디스크 읽기 요청
    filemap_get_read_batch(mapping, index, last_index - 1, fbatch);  // 다시 조회
}
```

- 페이지 크기는 4096 바이트이므로 오프셋 0~4095는 페이지 0이다.
- `filemap_get_read_batch(:2321)`는 `XA_STATE(xas, &mapping->i_pages, index)(:2324)`로 파일의 페이지 캐시 트리(xarray)에서 해당 번호의 folio를 RCU로 꺼낸다.
- **캐시 히트**: 디스크에 가지 않고 바로 복사로 넘어간다.
- **캐시 미스**: `page_cache_sync_readahead` → ext4_readahead → `submit_bio`

**캐시 상태에 따른 실행 경로:**

| 캐시 상태 | open에서 | read에서 |
|----------|---------|---------|
| dcache·페이지 캐시 모두 히트 | `lookup_fast`에서 dentry 발견. `ext4_lookup`, `ext4_iget` 미호출 | `filemap_get_read_batch`에서 folio 발견. readahead 미호출 |
| 모두 미스 | `lookup_open` → `ext4_lookup` → `ext4_iget` 호출 | `page_cache_sync_readahead` → `ext4_readahead` → `submit_bio` 호출 |

> `seq`로 파일을 쓴 직후에는 쓴 데이터와 dentry가 메모리에 남아 있으므로 히트 경로가 실행된다. 미스 경로를 관찰하려면: `sync; echo 3 | sudo tee /proc/sys/vm/drop_caches`

#### 미스일 때: readahead (mm/readahead.c)

```
page_cache_sync_readahead (include/linux/pagemap.h:1389)
└── page_cache_sync_ra (mm/readahead.c:538)
    └── [파일 처음(index == 0): "순차 읽기 시작"으로 판단]
        └── get_init_ra_size(:361): 창 크기 결정
            └── page_cache_ra_order → read_pages (:146)
                └── aops->readahead(rac) (:160)   ← 함수 포인터 위임
                    = ext4_readahead (fs/ext4/inode.c:3568)
```

#### ext4_readahead → ext4_mpage_readpages (fs/ext4/readpage.c:211)

```c
// fs/ext4/inode.c:3178
static void ext4_readahead(struct readahead_control *rac)
{
    ext4_mpage_readpages(rac->mapping->host, rac, NULL);
}
```

각 페이지에 대해:

1. **`ext4_map_blocks(NULL, inode, &map, 0)`** (`readpage.c:289`): 파일 내 논리 블록 번호를 디스크의 물리 블록 번호로 변환 (extent 트리 조회)
2. 물리 블록이 앞 페이지와 **연속**이면 같은 `bio`에 붙임, 아니면 `submit_bio`로 이전 bio를 전송
3. `bio_alloc(bdev, ..., REQ_OP_READ, ...)`: 새 bio 생성
4. **`submit_bio`**: 블록 계층에 읽기 요청 전달 → **필수 분석은 여기까지**

#### copy_folio_to_iter → raw_copy_to_user

커널의 페이지 캐시에서 사용자 버퍼(glibc stdio 버퍼)로 복사하는 경로:

```
copy_folio_to_iter        include/linux/uio.h:186
└── copy_page_to_iter     lib/iov_iter.c:349
    └── _copy_to_iter     lib/iov_iter.c:179
        └── copy_to_user_iter   lib/iov_iter.c:17
            └── raw_copy_to_user  arch/x86/include/asm/uaccess_64.h:145
                └── copy_user_generic                    :119
                    stac()                               :121  사용자 메모리 접근 허용 (SMAP)
                    rep movsb + _ASM_EXTABLE_UA(1b, 2b)  :131  바이트 복사 + 예외 테이블 등록
                    clac()                               :134  다시 금지
```

- **SMAP (Supervisor Mode Access Prevention)**: 커널 모드에서 사용자 페이지에 실수로 접근하는 것을 CPU가 막는 보호 기능. 의도된 복사 구간만 `stac`로 잠시 풀고 `clac`로 다시 잠근다.
- **예외 테이블**: 사용자가 준 주소가 매핑되지 않아 페이지 폴트가 나면 커널 패닉 대신 레이블 2로 점프. `_ASM_EXTABLE_UA`가 이를 등록한다.

> 과제 표의 `copy_to_user`는 이 경로에서 직접 호출되지 않는다. `copy_to_user`도 내부에서 `raw_copy_to_user`를 호출하므로 실제 복사 함수는 같다.

---

### B-4. 복귀와 두 번째 fread

#### 첫 번째 fread 복귀 과정

```
커널: read 시스템 콜이 4096 반환
  → sysret → glibc __read 반환 = 4096
    → _IO_new_file_underflow:
        fp->_IO_read_end = fp->_IO_buf_base + 4096   (버퍼에 4096바이트 채워짐)
      → _IO_new_file_xsgetn:
        memcpy(buf, fp->_IO_read_ptr, 100)           (100바이트를 프로그램의 buf로 복사)
        fp->_IO_read_ptr += 100                      (읽기 포인터 전진)
      → fread 반환: 100
```

**버퍼 상태 (첫 번째 fread 후):**

```
_IO_buf_base ──► [100 소비됨][3996 남음] ◄── _IO_buf_end (= _IO_read_end)
                              ↑
                        _IO_read_ptr
```

#### 두 번째 fread 경로 — 왜 다른가

```
fread(buf, 1, 100, fp)    [두 번째 fread]
└── _IO_fread → _IO_sgetn → _IO_new_file_xsgetn
    └── 버퍼 확인: fp->_IO_read_end - fp->_IO_read_ptr = 3996 > 100
        → memcpy(buf, fp->_IO_read_ptr, 100)     ← 버퍼에서 직접 복사
          fp->_IO_read_ptr += 100
        → 반환: 100   (시스템 콜 없음!)
```

**두 번째 fread 때 read 시스템 콜은 발생하지 않는다.**

`data.txt`는 약 28 KB이고, glibc 버퍼가 4096 바이트를 한 번에 채웠으므로, 첫 번째 `fread` 이후 버퍼에 3996 바이트가 남아 있다. 두 번째 `fread`는 100 바이트를 버퍼에서 바로 `memcpy`하고 끝난다.

| | 첫 번째 fread | 두 번째 fread |
|--|-------------|-------------|
| 버퍼 상태 | 비어 있음 | 3996 바이트 남음 |
| 시스템 콜 | `fstat`(버퍼 할당) + `read`(4096 bytes) | **없음** |
| 커널 함수 호출 | ext4_file_read_iter ~ submit_bio | **없음** |
| 데이터 출처 | 디스크(또는 페이지 캐시) | glibc 버퍼 메모리 |

> **이것이 stdio 버퍼링의 핵심 효과다.** fread 100번을 호출해도 시스템 콜은 처음 몇 번만 발생한다.

---

## 5. 실험 검증

### E1. strace: 시스템 콜 경계 확인

```bash
strace -e trace=openat,newfstatat,fstat,read,close ./fsread data.txt
```

**예상 출력:**

```
openat(AT_FDCWD, "data.txt", O_RDONLY) = 3          ← fopen의 openat
newfstatat(3, "", {st_size=28894, st_blksize=4096, ...}, AT_EMPTY_PATH) = 0
                                                     ← 버퍼 크기 결정용 fstat
read(3, "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n"..., 4096) = 4096
                                                     ← 첫 번째 fread에서 발생
                                                     ← 요청 바이트는 100이 아닌 4096!
read %zu + %zu bytes: 100 + 100
                                                     ← 두 번째 fread: read 없음
close(3)                                             = 0
```

**확인 포인트:**
- `openat`은 1회만 발생 (fopen에서)
- `read`의 요청 크기가 4096 (`st_blksize`) 이고 100이 아님 → 사용자 공간 버퍼링 효과
- 두 번째 fread에서 `read`가 **발생하지 않음** → 버퍼에서 바로 복사

### E2. gdb: syscall 직전 사용자 공간 상태

```bash
gdb -q --args ./fsread data.txt
(gdb) break main
(gdb) run
(gdb) catch syscall openat read
(gdb) continue
# openat syscall 진입 시 정지
(gdb) bt                          # 호출 스택
(gdb) p $orig_rax                 # syscall 번호 (257 = openat, 0 = read)
(gdb) info registers rax rdi rsi rdx r10
# rax=257, rdi=-100(AT_FDCWD), rsi=주소, rdx=플래그, r10=모드
(gdb) x/s $rsi                    # rsi가 가리키는 문자열 = "data.txt"
(gdb) x/3i $pc-4                  # syscall 명령 직전 명령 확인
```

**openat syscall 진입 시 레지스터:**

| 레지스터 | 값 | 의미 |
|---------|-----|------|
| `rax` | `257` | `__NR_openat` |
| `rdi` | `-100` | `AT_FDCWD` |
| `rsi` | `주소` | `"data.txt"` 포인터 |
| `rdx` | `0x8000` | `O_RDONLY\|O_LARGEFILE` |
| `r10` | `0x1b6` | `0666` |

### E3. bpftrace: 커널 호출 스택

터미널 1에서:
```bash
sudo bpftrace -e '
  kprobe:ext4_file_open      /comm == "fsread"/ {
    printf("== open ==\n%s\n", kstack);
  }
  kprobe:ext4_file_read_iter /comm == "fsread"/ {
    printf("== read ==\n%s\n", kstack);
  }'
```

터미널 2에서: `./fsread data.txt`

**예상 출력 (open):**
```
== open ==
        ext4_file_open+1
        do_dentry_open+498
        vfs_open+77
        do_open+136
        path_openat+1234
        do_filp_open+162
        do_sys_openat2+215
        do_sys_open+71
        __x64_sys_openat+26
        do_syscall_64+88
        entry_SYSCALL_64_after_hwframe+118
```

**예상 출력 (read):**
```
== read ==
        ext4_file_read_iter+1
        new_sync_read+205
        vfs_read+249
        ksys_read+104
        __x64_sys_read+26
        do_syscall_64+88
        entry_SYSCALL_64_after_hwframe+118
```

### E4. ftrace function_graph: 커널 내부 호출 트리

```bash
# openat 추적
sudo trace-cmd record -p function_graph \
    -g __x64_sys_openat --max-graph-depth 10 \
    -F ./fsread data.txt
trace-cmd report > openat_graph.txt

# read 추적
sudo trace-cmd record -p function_graph \
    -g __x64_sys_read --max-graph-depth 12 \
    -F ./fsread data.txt
trace-cmd report > read_graph.txt
```

**캐시 비운 후 미스 경로 확인:**
```bash
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches
sudo trace-cmd record -p function_graph -g __x64_sys_read \
    --max-graph-depth 15 -F ./fsread data.txt
trace-cmd report > read_graph_cold.txt
# ext4_readahead, ext4_mpage_readpages, submit_bio 가 보여야 함
```

---

## 6. 핵심 개념 정리

### VFS 다형성: "VFS는 ext4를 어떻게 아는가"

VFS는 파일 시스템마다 다른 동작을 **함수 테이블(함수 포인터 구조체)**로 감춘다. 이것이 C 언어로 구현한 다형성이다.

```
함수 테이블은 전역에 하나뿐, 그 주소가 두 단계로 전달됨:

① ext4_iget → inode->i_fop = &ext4_file_operations   (fs/ext4/inode.c:4960)
② do_dentry_open → f->f_op = fops_get(inode->i_fop)   (fs/open.c:939)

이후 VFS는 파일 시스템을 모른 채 f->f_op->...만 호출한다.
```

**발표에서 반드시 짚을 6개 줄:**

| 순서 | 줄 | 한 문장 설명 |
|------|---|------------|
| 1 | `inode->i_fop = &ext4_file_operations;` `fs/ext4/inode.c:4960` | ext4가 inode를 만들 때 자기 함수 테이블 주소를 꽂는다 |
| 2 | `f->f_op = fops_get(inode->i_fop);` `fs/open.c:939` | open할 때 그 주소를 struct file에도 적는다 |
| 3 | `open = f->f_op->open;` `fs/open.c:956` | 함수 포인터로 ext4_file_open을 호출한다 |
| 4 | `fd_install(fd, f);` `fs/open.c:1420` | 완성된 file을 fd 번호에 등록해 사용자에게 돌려준다 |
| 5 | `if (file->f_op->read) ... else if (file->f_op->read_iter)` `fs/read_write.c:566` | ext4는 .read가 NULL이라 read_iter 쪽으로 간다 |
| 6 | `ret = filp->f_op->read_iter(&kiocb, &iter);` `fs/read_write.c:488` | 함수 포인터로 ext4_file_read_iter를 호출한다 |

### 핵심 자료구조

| 구조체 | 무엇인가 | 담당 역할 |
|-------|---------|---------|
| `struct inode` | 파일 자체 (크기, 권한, 블록 위치). 이름 없음 | `i_fop`에 파일 시스템 함수 테이블 주소 보관 |
| `struct dentry` | 이름 하나와 inode의 연결. dcache에 캐시됨 | 경로 탐색이 "data.txt" → inode를 찾는 단위 |
| `struct file` | 열린 파일 하나. 오프셋 `f_pos`, `f_op` 보관 | open이 만들고 fd 테이블에 등록, read가 fd로 꺼냄 |
| `struct file_operations` | 함수 테이블 (포인터 모음). 전역에 하나뿐 | VFS가 `f_op->open`, `f_op->read_iter`로 호출 |
| `struct kiocb` | 비동기/동기 I/O 제어 블록 | 파일, 위치, 플래그를 read_iter에 전달 |
| `struct iov_iter` | 사용자 버퍼 주소·길이 반복자 | read → read_iter 형식 변환에 사용 |

### stdio 버퍼링이 시스템 콜 횟수를 바꾸는 이유

```
fread 호출 횟수 ≠ read 시스템 콜 횟수

예시 (data.txt = 28KB, buf = 100B, 버퍼 = 4096B):

1회 fread(buf, 1, 100) → fstat + read(4096) → 버퍼에 3996B 남음
2회 fread(buf, 1, 100) → memcpy만 (시스템 콜 없음)
...
41회 fread(buf, 1, 100) → memcpy만 (버퍼 소진 전까지)
42회 fread(buf, 1, 100) → read(4096) 다시 발생
```

---

## 7. 사용 도구

| 도구 / 리소스 | 용도 |
|-------------|------|
| **Bootlin Elixir (Linux v6.12)** | 커널 소스 탐색 및 함수 정의·호출처 교차 참조 |
| **Bootlin Elixir (glibc 2.39)** | glibc 소스 탐색 |
| **Claude (AI)** | 초기 함수 목록 생성, 보고서 초안 작성, 코드 설명. 모든 내용은 Elixir/GitHub v6.12 태그로 직접 검증함 |
| **strace** | 시스템 콜 경계 확인, openat/read 요청 크기·횟수 측정 |
| **gdb** | syscall 직전 레지스터 상태, 사용자 공간 호출 스택 확인 |
| **bpftrace** | 커널 진입점(ext4_file_open, ext4_file_read_iter)에서 kstack 덤프 |
| **trace-cmd (ftrace)** | 함수 호출 그래프, 캐시 히트·미스 경로 비교 |
| **Intel SDM / AMD APM** | syscall 명령 하드웨어 동작 (RCX, R11, MSR_LSTAR) 참조 |

---

*참조 소스: `fs/open.c`, `fs/namei.c`, `fs/read_write.c`, `fs/file.c`, `fs/ext4/file.c`, `fs/ext4/inode.c`, `fs/ext4/namei.c`, `fs/ext4/readpage.c`, `fs/quota/dquot.c`, `mm/filemap.c`, `mm/readahead.c`, `lib/iov_iter.c`, `libio/iofopen.c`, `libio/iofread.c`, `libio/fileops.c`, `libio/genops.c`, `arch/x86/entry/entry_64.S`, `arch/x86/entry/common.c`, `arch/x86/include/asm/syscall_wrapper.h`, `sysdeps/unix/sysv/linux/x86_64/sysdep.h`  
모든 줄 번호는 Linux v6.12 및 glibc 2.39 태그 기준.*
