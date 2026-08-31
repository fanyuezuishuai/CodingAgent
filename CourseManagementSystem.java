import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * ============================================================
 *  课程管理系统（单文件 Java 面向对象入门示例）
 * ============================================================
 *
 * 本文件为了演示 Java 面向对象的核心概念，刻意把多个类写在同一个文件里。
 * 注意：一个 .java 文件最多只能有一个 public 顶层类，且类名必须与文件名相同。
 * 其他顶层类不加 public，称为“包级私有类”。
 *
 * 这里覆盖的知识点：
 *   1. 类与对象
 *   2. 封装（private 字段 + public 方法访问）
 *   3. 继承（Student / Teacher 继承 Person）
 *   4. 多态（父类引用指向子类对象、抽象方法、接口实现）
 *   5. 抽象类（Person）
 *   6. 接口（Manageable，CourseManager 实现它）
 *   7. 枚举（CourseType）
 *   8. 集合与泛型（List、Map）
 *   9. 组合/关联（Course 里包含 Teacher 和多个 Student）
 *  10. 方法重写（toString、getRole）
 */
public class CourseManagementSystem {

    public static void main(String[] args) {
        // 1. 创建管理对象
        CourseManager manager = new CourseManager();

        // 2. 准备教师和学生（子类对象赋值给父类引用，体现多态）
        Person teacher1 = new Teacher("T001", "张老师", "zhang@school.edu.cn", "教授", "计算机学院");
        Person student1 = new Student("S001", "小明", "xiaoming@school.edu.cn", "软件工程", 2023);
        Person student2 = new Student("S002", "小红", "xiaohong@school.edu.cn", "计算机科学", 2024);
        Person student3 = new Student("S003", "小刚", "xiaogang@school.edu.cn", "人工智能", 2022);

        // 把 Person 放进数组，运行时调用各自重写后的 getRole()，体现多态
        Person[] people = {teacher1, student1, student2, student3};
        System.out.println("========== 人员信息 ==========");
        for (Person person : people) {
            System.out.println(person);
            System.out.println("角色：" + person.getRole());
            System.out.println("------------------------------");
        }

        // 3. 添加到管理系统
        manager.addTeacher((Teacher) teacher1);
        manager.addStudent((Student) student1);
        manager.addStudent((Student) student2);
        manager.addStudent((Student) student3);

        // 4. 创建课程（课程关联教师，体现组合关系）
        Course javaCourse = new Course(
                "C001",
                "Java 面向对象编程",
                3,
                CourseType.REQUIRED,
                (Teacher) teacher1,
                2  // 容量故意设为 2，用来演示选满和选课失败
        );
        Course algorithmCourse = new Course(
                "C002",
                "数据结构与算法",
                4,
                CourseType.REQUIRED,
                (Teacher) teacher1,
                30
        );

        manager.addCourse(javaCourse);
        manager.addCourse(algorithmCourse);

        // 5. 选课操作
        System.out.println("========== 选课操作 ==========");
        System.out.println("选课结果：" + manager.enroll("S001", "C001"));
        System.out.println("选课结果：" + manager.enroll("S002", "C001"));
        // 容量已满，应该失败
        System.out.println("选课结果：" + manager.enroll("S003", "C001"));
        // 重复选同一门课，应该失败
        System.out.println("选课结果：" + manager.enroll("S001", "C001"));

        System.out.println("========== 退课操作 ==========");
        System.out.println("退课结果：" + manager.drop("S002", "C001"));
        // 退课成功后空出名额，小刚可以选上
        System.out.println("再次选课结果：" + manager.enroll("S003", "C001"));

        // 6. 打印所有课程及选课名单
        System.out.println("========== 课程信息 ==========");
        manager.showAllCourses();
    }
}

/**
 * 课程类型枚举。
 * 枚举是一种特殊的类，用于限定取值，例如：必修、选修。
 */
enum CourseType {
    REQUIRED("必修"),
    ELECTIVE("选修");

    private final String displayName;

    CourseType(String displayName) {
        this.displayName = displayName;
    }

    public String getDisplayName() {
        return displayName;
    }
}

/**
 * 抽象类：人。
 * 抽象类不能直接 new，只能被继承。它可以包含抽象方法，也可以包含普通方法。
 */
abstract class Person {
    private final String id;   // final 表示 id 一旦赋值不能修改
    private String name;
    private String email;

    public Person(String id, String name, String email) {
        // 构造时做简单校验，保证对象一创建就是合法的
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("编号不能为空");
        }
        if (name == null || name.isBlank()) {
            throw new IllegalArgumentException("姓名不能为空");
        }
        this.id = id;
        this.name = name;
        this.email = email;
    }

    public String getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        if (name == null || name.isBlank()) {
            throw new IllegalArgumentException("姓名不能为空");
        }
        this.name = name;
    }

    public String getEmail() {
        return email;
    }

    public void setEmail(String email) {
        this.email = email;
    }

    /**
     * 抽象方法：子类必须实现，用来说明自己的角色。
     * 这就是多态的一个关键点：父类定义规范，子类提供具体实现。
     */
    public abstract String getRole();

    @Override
    public String toString() {
        return "编号=" + id + ", 姓名=" + name + ", 邮箱=" + email;
    }
}

/**
 * 学生类，继承 Person。
 * extends 表示“是一种”关系：Student is a Person。
 */
class Student extends Person {
    private String major;  // 专业
    private int grade;     // 入学年份

    public Student(String id, String name, String email, String major, int grade) {
        super(id, name, email); // 先调用父类构造器
        this.major = major;
        this.grade = grade;
    }

    public String getMajor() {
        return major;
    }

    public void setMajor(String major) {
        this.major = major;
    }

    public int getGrade() {
        return grade;
    }

    public void setGrade(int grade) {
        this.grade = grade;
    }

    @Override
    public String getRole() {
        return "学生";
    }

    @Override
    public String toString() {
        // 复用父类公共信息，再补充子类特有信息
        return super.toString() + ", 专业=" + major + ", 年级=" + grade;
    }
}

/**
 * 教师类，继承 Person。
 */
class Teacher extends Person {
    private String title;      // 职称，如：教授、副教授
    private String department; // 院系

    public Teacher(String id, String name, String email, String title, String department) {
        super(id, name, email);
        this.title = title;
        this.department = department;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getDepartment() {
        return department;
    }

    public void setDepartment(String department) {
        this.department = department;
    }

    @Override
    public String getRole() {
        return "教师";
    }

    @Override
    public String toString() {
        return super.toString() + ", 职称=" + title + ", 院系=" + department;
    }
}

/**
 * 课程类。
 * 课程“拥有”一名授课教师和若干名选课学生，这种关系称为组合/关联。
 * 课程自己维护容量和选课名单，外部不能直接修改内部集合，这就是封装。
 */
class Course {
    private final String courseId;
    private String courseName;
    private int credit;
    private CourseType type;
    private Teacher teacher;
    private final int capacity;
    private final List<Student> enrolledStudents;

    public Course(String courseId,
                  String courseName,
                  int credit,
                  CourseType type,
                  Teacher teacher,
                  int capacity) {
        if (courseId == null || courseId.isBlank()) {
            throw new IllegalArgumentException("课程编号不能为空");
        }
        if (capacity <= 0) {
            throw new IllegalArgumentException("课程容量必须大于 0");
        }
        this.courseId = courseId;
        this.courseName = courseName;
        this.credit = credit;
        this.type = type;
        this.teacher = teacher;
        this.capacity = capacity;
        this.enrolledStudents = new ArrayList<>();
    }

    public String getCourseId() {
        return courseId;
    }

    public String getCourseName() {
        return courseName;
    }

    public void setCourseName(String courseName) {
        this.courseName = courseName;
    }

    public int getCredit() {
        return credit;
    }

    public void setCredit(int credit) {
        this.credit = credit;
    }

    public CourseType getType() {
        return type;
    }

    public void setType(CourseType type) {
        this.type = type;
    }

    public Teacher getTeacher() {
        return teacher;
    }

    public void setTeacher(Teacher teacher) {
        this.teacher = teacher;
    }

    public int getCapacity() {
        return capacity;
    }

    public int getEnrolledCount() {
        return enrolledStudents.size();
    }

    public boolean isFull() {
        return enrolledStudents.size() >= capacity;
    }

    /**
     * 选课：判断是否已满、是否重复选课。
     */
    public boolean enroll(Student student) {
        Objects.requireNonNull(student, "学生不能为 null");
        if (isFull()) {
            return false;
        }
        if (enrolledStudents.contains(student)) {
            return false;
        }
        enrolledStudents.add(student);
        return true;
    }

    /**
     * 退课。
     */
    public boolean drop(Student student) {
        Objects.requireNonNull(student, "学生不能为 null");
        return enrolledStudents.remove(student);
    }

    /**
     * 返回选课名单的副本，防止外部随意修改课程内部集合。
     */
    public List<Student> getEnrolledStudents() {
        return Collections.unmodifiableList(enrolledStudents);
    }

    @Override
    public String toString() {
        return "课程编号=" + courseId
                + ", 课程名=" + courseName
                + ", 学分=" + credit
                + ", 类型=" + type.getDisplayName()
                + ", 授课教师=" + (teacher == null ? "未指定" : teacher.getName())
                + ", 容量=" + capacity
                + ", 已选人数=" + getEnrolledCount();
    }
}

/**
 * 管理接口。
 * 接口只定义“能做什么”，不定义“怎么做”。实现类必须提供具体逻辑。
 * 面向接口编程可以让程序更容易替换实现，例如以后可以写 DatabaseCourseManager。
 */
interface Manageable {
    void addStudent(Student student);
    Student findStudentById(String studentId);
    void addTeacher(Teacher teacher);
    Teacher findTeacherById(String teacherId);
    void addCourse(Course course);
    Course findCourseById(String courseId);
    boolean enroll(String studentId, String courseId);
    boolean drop(String studentId, String courseId);
    void showAllCourses();
}

/**
 * 课程管理器，负责学生、教师和课程的管理。
 * implements Manageable 表示“实现接口”，必须实现接口中的所有方法。
 */
class CourseManager implements Manageable {
    // 使用 Map 保存学生，可以快速根据 id 查找
    private final Map<String, Student> students = new LinkedHashMap<>();
    private final Map<String, Teacher> teachers = new LinkedHashMap<>();
    private final List<Course> courses = new ArrayList<>();

    @Override
    public void addStudent(Student student) {
        Objects.requireNonNull(student, "学生不能为 null");
        if (students.containsKey(student.getId())) {
            throw new IllegalArgumentException("学生编号已存在：" + student.getId());
        }
        students.put(student.getId(), student);
    }

    @Override
    public Student findStudentById(String studentId) {
        return students.get(studentId);
    }

    @Override
    public void addTeacher(Teacher teacher) {
        Objects.requireNonNull(teacher, "教师不能为 null");
        if (teachers.containsKey(teacher.getId())) {
            throw new IllegalArgumentException("教师编号已存在：" + teacher.getId());
        }
        teachers.put(teacher.getId(), teacher);
    }

    @Override
    public Teacher findTeacherById(String teacherId) {
        return teachers.get(teacherId);
    }

    @Override
    public void addCourse(Course course) {
        Objects.requireNonNull(course, "课程不能为 null");
        if (findCourseById(course.getCourseId()) != null) {
            throw new IllegalArgumentException("课程编号已存在：" + course.getCourseId());
        }
        courses.add(course);
    }

    @Override
    public Course findCourseById(String courseId) {
        for (Course course : courses) {
            if (course.getCourseId().equals(courseId)) {
                return course;
            }
        }
        return null;
    }

    /**
     * 学生选课：把“查找”和“选课”拆开，逻辑清晰。
     */
    @Override
    public boolean enroll(String studentId, String courseId) {
        Student student = findStudentById(studentId);
        Course course = findCourseById(courseId);

        if (student == null) {
            System.out.println("选课失败：学生不存在 -> " + studentId);
            return false;
        }
        if (course == null) {
            System.out.println("选课失败：课程不存在 -> " + courseId);
            return false;
        }
        return course.enroll(student);
    }

    /**
     * 学生退课。
     */
    @Override
    public boolean drop(String studentId, String courseId) {
        Student student = findStudentById(studentId);
        Course course = findCourseById(courseId);

        if (student == null) {
            System.out.println("退课失败：学生不存在 -> " + studentId);
            return false;
        }
        if (course == null) {
            System.out.println("退课失败：课程不存在 -> " + courseId);
            return false;
        }
        return course.drop(student);
    }

    @Override
    public void showAllCourses() {
        if (courses.isEmpty()) {
            System.out.println("当前没有课程。");
            return;
        }

        for (Course course : courses) {
            System.out.println(course);
            List<Student> enrolledStudents = course.getEnrolledStudents();
            if (enrolledStudents.isEmpty()) {
                System.out.println("   选课学生：暂无");
            } else {
                System.out.println("   选课学生：");
                for (Student student : enrolledStudents) {
                    System.out.println("      - " + student.getName() + "（" + student.getId() + "）");
                }
            }
            System.out.println("------------------------------");
        }
    }
}
