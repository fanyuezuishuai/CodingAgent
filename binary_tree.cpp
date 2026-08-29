#include <iostream>

// 二叉树节点结构体
struct TreeNode {
    int data;
    TreeNode* left;
    TreeNode* right;

    explicit TreeNode(int value) : data(value), left(nullptr), right(nullptr) {}
};

// 二叉树类（面向对象封装）
class BinaryTree {
public:
    // 构造函数：初始化为空树
    BinaryTree() : root_(nullptr) {}

    // 析构函数：释放整棵树的所有节点
    ~BinaryTree() {
        destroy(root_);
    }

    // 创建二叉搜索树：按二叉搜索树规则插入一个节点
    void insert(int value) {
        root_ = insertRecursive(root_, value);
    }

    // 创建普通二叉树：从前序遍历序列创建，-1 表示空节点
    void createFromPreorder() {
        destroy(root_);
        root_ = nullptr;
        std::cout << "请输入前序遍历序列（空节点用 -1 表示），例如：1 2 -1 -1 3 -1 -1\n";
        root_ = buildFromPreorder();
    }

    // 中序遍历：左 -> 根 -> 右
    void inorderTraversal() const {
        std::cout << "中序遍历结果: ";
        inorderRecursive(root_);
        std::cout << std::endl;
    }

private:
    TreeNode* root_;

    // 按二叉搜索树规则递归插入
    TreeNode* insertRecursive(TreeNode* node, int value) {
        if (node == nullptr) {
            return new TreeNode(value);
        }

        if (value < node->data) {
            node->left = insertRecursive(node->left, value);
        } else if (value > node->data) {
            node->right = insertRecursive(node->right, value);
        }
        // 如果 value == node->data，可以选择忽略重复值

        return node;
    }

    // 从前序输入序列递归创建二叉树
    TreeNode* buildFromPreorder() {
        int value;
        std::cin >> value;

        if (value == -1) {
            return nullptr;
        }

        TreeNode* node = new TreeNode(value);
        node->left = buildFromPreorder();
        node->right = buildFromPreorder();
        return node;
    }

    // 递归中序遍历
    void inorderRecursive(TreeNode* node) const {
        if (node == nullptr) {
            return;
        }

        inorderRecursive(node->left);
        std::cout << node->data << " ";
        inorderRecursive(node->right);
    }

    // 递归销毁二叉树
    void destroy(TreeNode* node) {
        if (node == nullptr) {
            return;
        }

        destroy(node->left);
        destroy(node->right);
        delete node;
    }
};

int main() {
    BinaryTree tree;

    // 方式一：创建二叉搜索树
    tree.insert(5);
    tree.insert(3);
    tree.insert(8);
    tree.insert(1);
    tree.insert(4);
    tree.insert(7);
    tree.insert(9);
    tree.inorderTraversal();  // 输出：1 3 4 5 7 8 9

    // 方式二：从前序序列创建普通二叉树
    // 输入示例：1 2 -1 -1 3 -1 -1 会创建：
    /*
     *       1
     *      / \
     *     2   3
     */
    // tree.createFromPreorder();
    // tree.inorderTraversal();

    return 0;
}
