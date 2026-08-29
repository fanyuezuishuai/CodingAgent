#include <iostream>
#include <vector>

// 迭代法求斐波那契数列：fib(0)=0, fib(1)=1
// 返回前 n 项（n >= 1）
std::vector<unsigned long long> fibonacci(int n) {
    std::vector<unsigned long long> fib;
    if (n <= 0) {
        return fib;
    }

    fib.push_back(0);  // 第 0 项
    if (n == 1) {
        return fib;
    }

    fib.push_back(1);  // 第 1 项
    for (int i = 2; i < n; ++i) {
        unsigned long long next = fib[i - 1] + fib[i - 2];
        fib.push_back(next);
    }
    return fib;
}

int main() {
    int n;
    std::cout << "请输入要输出的斐波那契数列项数 n: ";
    std::cin >> n;

    std::vector<unsigned long long> fib = fibonacci(n);

    std::cout << "斐波那契数列前 " << n << " 项为:\n";
    for (std::size_t i = 0; i < fib.size(); ++i) {
        std::cout << fib[i];
        if (i + 1 < fib.size()) {
            std::cout << ", ";
        }
    }
    std::cout << std::endl;

    return 0;
}
